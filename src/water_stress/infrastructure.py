from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Literal

Infrastructure = Literal["local", "aws"]


def run_command(arguments: list[str], *, environment: dict[str, str] | None = None) -> str:
    """Run infrastructure tools without shell expansion or printing credentials."""
    if shutil.which(arguments[0]) is None:
        raise RuntimeError(f"Install {arguments[0]} before running this command")
    try:
        result = subprocess.run(
            arguments, check=True, capture_output=True, text=True, timeout=3600, env=environment
        )
    except subprocess.TimeoutExpired as error:
        raise RuntimeError(f"{arguments[0]} timed out; check status before retrying") from error
    except subprocess.CalledProcessError as error:
        # Child stderr may contain connection URIs. Leave detailed diagnosis to the
        # user's terminal, rather than copying secrets into structured logs.
        raise RuntimeError(
            f"{arguments[0]} failed (exit {error.returncode}); check credentials, permissions "
            "and service status in your terminal"
        ) from error
    return result.stdout


def compose_command(infrastructure: Infrastructure, env_file: Path) -> list[str]:
    command = ["docker", "compose", "--env-file", str(env_file)]
    if infrastructure == "aws":
        command += ["--project-name", "water-stress-aws"]
    command += ["-f", "compose.airflow.yml"]
    if infrastructure == "aws":
        command += ["-f", "compose.aws.yml"]
    return command


def run_services(infrastructure: Infrastructure, action: str, env_file: Path) -> str:
    if not env_file.is_file():
        example = (
            "deployment/aws/aws.env.example"
            if infrastructure == "aws"
            else ("deployment/airflow/airflow.env.example")
        )
        raise ValueError(f"Create {env_file} from {example} and fill in its passwords")
    command = compose_command(infrastructure, env_file)
    if action == "up":
        command += ["up", "-d", "--build", "--wait", "--wait-timeout", "300"]
    elif action == "stop":
        command += ["stop"]
    elif action == "status":
        command += ["ps", "--all"]
    elif action == "load":
        # Use the existing loader, never ingestion or transformation. The same
        # dataset order and transactional restart policy apply on both hosts.
        command += [
            "run",
            "--rm",
            "--no-deps",
            "--entrypoint",
            "/opt/pipeline-venv/bin/python",
            "airflow",
            "-m",
            "water_stress.pipelines.run_database",
            "--migrate",
            "--register-bronze",
            "--load",
            "--dataset",
            "all",
        ]
    else:
        raise ValueError(f"Unsupported infrastructure action: {action}")
    environment = os.environ.copy()
    environment["WATER_STRESS_DEPLOYMENT_ENV_FILE"] = str(env_file.resolve())
    return run_command(command, environment=environment)

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import dotenv_values

ROOT = Path(__file__).parents[1]
COMPOSE = ROOT / "compose.production.yml"
DATABASE_ROLES = {
    "TASK_QUERY_DATABASE_URL": "auraclaw_task_query_ro",
    "SESSION_DATABASE_URL": "auraclaw_session",
    "PROJECTION_DATABASE_URL": "auraclaw_projection",
    "CONTROL_DATABASE_URL": "auraclaw_control",
    "HANDS_DATABASE_URL": "auraclaw_hands",
    "POLICY_DATABASE_URL": "auraclaw_policy",
    "CREDENTIAL_DATABASE_URL": "auraclaw_credential",
    "ARTIFACT_DATABASE_URL": "auraclaw_artifact",
    "STREAMING_DATABASE_URL": "auraclaw_streaming",
    "MODEL_DATABASE_URL": "auraclaw_model",
    "DELIVERY_DATABASE_URL": "auraclaw_delivery",
}
WORKLOAD_TOKENS = (
    "AURACLAW_TASK_API_WORKLOAD_TOKEN",
    "AURACLAW_PROJECTION_WORKLOAD_TOKEN",
    "AURACLAW_ORCHESTRATOR_WORKLOAD_TOKEN",
    "AURACLAW_RUNTIME_WORKLOAD_TOKEN",
    "AURACLAW_MODEL_GATEWAY_WORKLOAD_TOKEN",
    "AURACLAW_ACTION_HANDS_WORKLOAD_TOKEN",
    "AURACLAW_CREDENTIAL_PROXY_WORKLOAD_TOKEN",
    "AURACLAW_ARTIFACT_SERVICE_WORKLOAD_TOKEN",
    "AURACLAW_POLICY_WORKLOAD_TOKEN",
    "AURACLAW_DELIVERY_WORKLOAD_TOKEN",
)
JAVA_WORKLOAD_CONFIGURATION = (
    "AURACLAW_JAVA_AGENT_RUNTIME_BASE_URL",
)
REQUIRED = (
    "AURACLAW_IMAGE",
    "AURACLAW_MIGRATION_DATABASE_URL",
    *DATABASE_ROLES,
    *WORKLOAD_TOKENS,
    "AURACLAW_LEASE_SIGNING_KEY",
    "AURACLAW_MODEL_API_KEY",
    "AURACLAW_MODEL_BASE_URL",
    "AURACLAW_MODEL_NAME",
    "AURACLAW_CREDENTIAL_VAULT_ADDR",
    "AURACLAW_CREDENTIAL_VAULT_TOKEN",
    "SEAWEEDFS_HOST",
    "SEAWEEDFS_ACCESS_KEY",
    "SEAWEEDFS_SECRET_KEY",
)


def _dsn_username(value: str) -> str | None:
    normalized = (
        value.replace("postgresql+asyncpg://", "postgresql://")
        .replace("mysql+aiomysql://", "mysql://")
        .replace("mysql+asyncmy://", "mysql://")
        .replace("mysql+pymysql://", "mysql://")
    )
    return urlsplit(normalized).username


def main() -> int:
    parser = argparse.ArgumentParser(description="validate AuraClaw production Compose inputs")
    parser.add_argument("--env-file", default=".env.production")
    args = parser.parse_args()
    env_path = Path(args.env_file)
    if not env_path.is_file():
        print(f"preflight failed: env file not found: {env_path}")
        return 1
    file_values = dotenv_values(env_path)
    def configured_value(name: str) -> str:
        """Read one non-secret deployment value without logging its contents."""
        return os.environ.get(name) or file_values.get(name) or ""

    values = {
        name: os.environ.get(name) or file_values.get(name) or "" for name in REQUIRED
    }
    failures = [f"missing {name}" for name in REQUIRED if not values[name]]

    image = values["AURACLAW_IMAGE"]
    if image and (
        image.endswith(":latest")
        or "replace-with-immutable-sha" in image
        or ":" not in image.split("/")[-1]
    ):
        failures.append("AURACLAW_IMAGE must use an immutable digest or version/SHA tag")

    role_urls: set[str] = set()
    for variable, expected_role in DATABASE_ROLES.items():
        value = values[variable]
        if not value:
            continue
        username = _dsn_username(value)
        if username != expected_role:
            failures.append(f"{variable} must authenticate as {expected_role}")
        if value in role_urls:
            failures.append(f"{variable} reuses another service DSN")
        role_urls.add(value)

    token_values = [values[name] for name in WORKLOAD_TOKENS if values[name]]
    if any(len(value) < 32 for value in token_values):
        failures.append("workload tokens must contain at least 32 characters")
    if len(token_values) != len(set(token_values)):
        failures.append("workload tokens must be unique per service identity")
    lease_key = values["AURACLAW_LEASE_SIGNING_KEY"]
    if lease_key and len(lease_key) < 32:
        failures.append("AURACLAW_LEASE_SIGNING_KEY must contain at least 32 characters")

    price_insight_backend = (
        configured_value("AURACLAW_PRICE_INSIGHT_TOOL_BACKEND") or "java"
    )
    if price_insight_backend == "java":
        # Java is the default atomic-Tool path. Fail closed before Compose
        # starts if the shared workload credential is incomplete; the Python
        # process must never silently fall back to the in-process calculator.
        failures.extend(
            f"missing {name} for Java Price Insight backend"
            for name in JAVA_WORKLOAD_CONFIGURATION
            if not configured_value(name)
        )
        workload_token = configured_value(
            "AURACLAW_JAVA_AGENT_RUNTIME_WORKLOAD_TOKEN"
        )
        workload_token_file = configured_value(
            "AURACLAW_JAVA_AGENT_RUNTIME_WORKLOAD_TOKEN_FILE"
        )
        if not workload_token and not workload_token_file:
            failures.append(
                "missing AURACLAW_JAVA_AGENT_RUNTIME_WORKLOAD_TOKEN_FILE "
                "for Java Price Insight backend"
            )
        elif not workload_token and workload_token_file:
            source = Path(workload_token_file)
            if not source.is_absolute():
                source = env_path.parent / source
            if not source.is_file():
                failures.append(
                    "AURACLAW_JAVA_AGENT_RUNTIME_WORKLOAD_TOKEN_FILE "
                    "does not point to a readable file"
                )
            else:
                # Read only to enforce credential strength. The value is never
                # printed or included in a diagnostic message.
                workload_token = source.read_text().rstrip("\r\n")
        if workload_token and len(workload_token) < 32:
            failures.append(
                "AURACLAW_JAVA_AGENT_RUNTIME_WORKLOAD_TOKEN must contain at least 32 characters"
            )

    completed = subprocess.run(
        [
            "docker",
            "compose",
            "--env-file",
            str(env_path),
            "-f",
            str(COMPOSE),
            "--profile",
            "migrate",
            "config",
            "--quiet",
        ],
        cwd=ROOT,
        check=False,
    )
    if completed.returncode:
        failures.append("docker compose config validation failed")
    if failures:
        print("production Compose preflight failed")
        for failure in failures:
            print(f"- {failure}")
        return 1
    print("production Compose preflight passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())

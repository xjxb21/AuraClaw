from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from dotenv import dotenv_values

SECRET_VARIABLES = {
    "task_query_database_url": "TASK_QUERY_DATABASE_URL",
    "session_database_url": "SESSION_DATABASE_URL",
    "projection_database_url": "PROJECTION_DATABASE_URL",
    "control_database_url": "CONTROL_DATABASE_URL",
    "model_database_url": "MODEL_DATABASE_URL",
    "hands_database_url": "HANDS_DATABASE_URL",
    "policy_database_url": "POLICY_DATABASE_URL",
    "credential_database_url": "CREDENTIAL_DATABASE_URL",
    "artifact_database_url": "ARTIFACT_DATABASE_URL",
    "streaming_database_url": "STREAMING_DATABASE_URL",
    "delivery_database_url": "DELIVERY_DATABASE_URL",
    "migration_database_url": "AURACLAW_MIGRATION_DATABASE_URL",
    "task_api_workload_token": "AURACLAW_TASK_API_WORKLOAD_TOKEN",
    "projection_workload_token": "AURACLAW_PROJECTION_WORKLOAD_TOKEN",
    "orchestrator_workload_token": "AURACLAW_ORCHESTRATOR_WORKLOAD_TOKEN",
    "runtime_workload_token": "AURACLAW_RUNTIME_WORKLOAD_TOKEN",
    "model_gateway_workload_token": "AURACLAW_MODEL_GATEWAY_WORKLOAD_TOKEN",
    "action_hands_workload_token": "AURACLAW_ACTION_HANDS_WORKLOAD_TOKEN",
    "policy_workload_token": "AURACLAW_POLICY_WORKLOAD_TOKEN",
    "credential_proxy_workload_token": "AURACLAW_CREDENTIAL_PROXY_WORKLOAD_TOKEN",
    "artifact_service_workload_token": "AURACLAW_ARTIFACT_SERVICE_WORKLOAD_TOKEN",
    "delivery_workload_token": "AURACLAW_DELIVERY_WORKLOAD_TOKEN",
    "lease_signing_key": "AURACLAW_LEASE_SIGNING_KEY",
    "model_api_key": "AURACLAW_MODEL_API_KEY",
    "java_agent_runtime_workload_token": (
        "AURACLAW_JAVA_AGENT_RUNTIME_WORKLOAD_TOKEN"
    ),
    "vault_token": "AURACLAW_CREDENTIAL_VAULT_TOKEN",
    "seaweedfs_access_key": "SEAWEEDFS_ACCESS_KEY",
    "seaweedfs_secret_key": "SEAWEEDFS_SECRET_KEY",
}


def _secret_value(
    variable: str,
    configured: dict[str, str | None],
    *,
    env_file: Path,
) -> str:
    """Resolve one deployment secret from a direct value or a sibling `_FILE`.

    Direct environment values retain precedence for compatibility. File paths
    are resolved relative to the selected env file so deployment manifests can
    refer to a private key kept beside, but never embedded in, that manifest.
    """
    direct_value = os.environ.get(variable) or configured.get(variable)
    if direct_value:
        return direct_value

    file_variable = f"{variable}_FILE"
    file_value = os.environ.get(file_variable) or configured.get(file_variable)
    if not file_value:
        return ""
    source = Path(file_value)
    if not source.is_absolute():
        source = env_file.parent / source
    if not source.is_file():
        raise ValueError(f"secret source file is unavailable for {variable}")
    # Match the runtime loader's bound so an accidental large file cannot be
    # copied into a Compose secret or consumed into process memory.
    if source.stat().st_size > 64 * 1024:
        raise ValueError(f"secret source file is too large for {variable}")
    value = source.read_text().rstrip("\r\n")
    if not value:
        raise ValueError(f"secret source file is empty for {variable}")
    return value


def main() -> int:
    parser = argparse.ArgumentParser(
        description="materialize ignored 0600 files for Docker Compose secrets"
    )
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--output-dir", default=".runtime/compose-secrets")
    args = parser.parse_args()
    env_file = Path(args.env_file)
    if not env_file.is_file():
        print(f"secret materialization failed: env file not found: {env_file}")
        return 1
    configured = dotenv_values(env_file)
    try:
        values = {
            variable: _secret_value(variable, configured, env_file=env_file)
            for variable in SECRET_VARIABLES.values()
        }
    except ValueError as exc:
        # Report only the variable-level failure. Secret contents are never
        # included in diagnostics or exception tracebacks.
        print(f"secret materialization failed: {exc}")
        return 1
    missing = [variable for variable, value in values.items() if not value]
    if missing:
        print("secret materialization failed")
        for variable in missing:
            print(f"- missing {variable}")
        return 1

    output_dir = Path(args.output_dir)
    output_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    output_dir.chmod(0o700)
    for filename, variable in SECRET_VARIABLES.items():
        target = output_dir / filename
        temporary = output_dir / f".{filename}.tmp"
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
            0o600,
        )
        try:
            os.write(descriptor, values[variable].encode())
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        temporary.replace(target)
        target.chmod(0o600)
    print(f"materialized {len(SECRET_VARIABLES)} Compose secret files")
    return 0


if __name__ == "__main__":
    sys.exit(main())

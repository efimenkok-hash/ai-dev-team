#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

from core.env_layout import SharedRuntimeEnvConfig  # noqa: E402
from core.state_db_backup import (  # noqa: E402
    StateDbBackupConfig,
    StateDbBackupError,
    create_state_db_backup,
    default_state_db_backup_dir,
)

_CONFIG_FAILURE_EXIT_CODE = 2
_BACKUP_FAILURE_EXIT_CODE = 1


def _load_shared_runtime_env() -> SharedRuntimeEnvConfig:
    load_dotenv(dotenv_path=ROOT / ".env")
    return SharedRuntimeEnvConfig.from_env(os.environ)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Create a verified local SQLite backup of state.db.",
    )
    parser.add_argument(
        "--backup-dir",
        help=(
            "Optional target directory for backup artifacts. "
            "Defaults to a deterministic sibling `state-db-backups/` directory."
        ),
    )
    args = parser.parse_args(argv)

    try:
        runtime_env = _load_shared_runtime_env()
    except ValueError as exc:
        print(
            f"backup_state_db_failed:invalid_runtime_env: {exc}",
            file=sys.stderr,
        )
        return _CONFIG_FAILURE_EXIT_CODE

    source_state_db_path = runtime_env.state_db_path
    try:
        backup_dir = (
            Path(args.backup_dir).expanduser()
            if args.backup_dir is not None
            else default_state_db_backup_dir(source_state_db_path)
        )
        config = StateDbBackupConfig(
            source_state_db_path=source_state_db_path,
            backup_dir=backup_dir,
        )
    except ValueError as exc:
        print(
            f"backup_state_db_failed:invalid_backup_config: {exc}",
            file=sys.stderr,
        )
        return _CONFIG_FAILURE_EXIT_CODE

    try:
        result = create_state_db_backup(config)
    except StateDbBackupError as exc:
        print(
            f"backup_state_db_failed:{exc.code}: {exc}",
            file=sys.stderr,
        )
        return _BACKUP_FAILURE_EXIT_CODE

    print(
        json.dumps(
            result.to_dict(),
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

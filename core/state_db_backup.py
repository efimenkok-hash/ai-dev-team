from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

_SQLITE_TIMEOUT_SECONDS = 30.0
_DEFAULT_BACKUP_FILENAME_PREFIX = "state-db"


def _normalize_path(path: Path, *, field_name: str) -> Path:
    if not isinstance(path, Path):
        raise ValueError(f"invalid_{field_name}_type:{type(path).__name__}")
    expanded = path.expanduser()
    if expanded == Path(".") or not str(expanded).strip():
        raise ValueError(f"empty_{field_name}")
    return expanded


@dataclass(frozen=True)
class StateDbBackupConfig:
    source_state_db_path: Path
    backup_dir: Path
    write_manifest: bool = True
    filename_prefix: str = _DEFAULT_BACKUP_FILENAME_PREFIX

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "source_state_db_path",
            _normalize_path(
                self.source_state_db_path,
                field_name="source_state_db_path",
            ),
        )
        object.__setattr__(
            self,
            "backup_dir",
            _normalize_path(
                self.backup_dir,
                field_name="backup_dir",
            ),
        )
        if not isinstance(self.write_manifest, bool):
            raise ValueError(
                f"invalid_write_manifest_type:{type(self.write_manifest).__name__}"
            )
        if not isinstance(self.filename_prefix, str) or not self.filename_prefix.strip():
            raise ValueError("empty_filename_prefix")
        object.__setattr__(self, "filename_prefix", self.filename_prefix.strip())


@dataclass(frozen=True)
class StateDbBackupArtifact:
    backup_path: Path
    manifest_path: Path | None
    created_at_utc: str
    schema_version: int
    source_state_db_path: Path
    size_bytes: int
    sha256: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "backup_path",
            _normalize_path(self.backup_path, field_name="backup_path"),
        )
        if self.manifest_path is not None:
            object.__setattr__(
                self,
                "manifest_path",
                _normalize_path(self.manifest_path, field_name="manifest_path"),
            )
        if not isinstance(self.created_at_utc, str) or not self.created_at_utc.strip():
            raise ValueError("empty_created_at_utc")
        object.__setattr__(self, "created_at_utc", self.created_at_utc.strip())
        if isinstance(self.schema_version, bool) or not isinstance(self.schema_version, int):
            raise ValueError(f"invalid_schema_version:{self.schema_version!r}")
        if self.schema_version < 1:
            raise ValueError(f"invalid_schema_version:{self.schema_version!r}")
        object.__setattr__(
            self,
            "source_state_db_path",
            _normalize_path(
                self.source_state_db_path,
                field_name="source_state_db_path",
            ),
        )
        if isinstance(self.size_bytes, bool) or not isinstance(self.size_bytes, int):
            raise ValueError(f"invalid_size_bytes:{self.size_bytes!r}")
        if self.size_bytes < 0:
            raise ValueError(f"invalid_size_bytes:{self.size_bytes!r}")
        if self.sha256 is not None:
            if not isinstance(self.sha256, str) or not self.sha256.strip():
                raise ValueError("invalid_sha256")
            object.__setattr__(self, "sha256", self.sha256.strip())

    def to_manifest_dict(self) -> dict[str, object]:
        return {
            "backup_path": str(self.backup_path),
            "created_at_utc": self.created_at_utc,
            "schema_version": self.schema_version,
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
            "source_state_db_path": str(self.source_state_db_path),
        }


@dataclass(frozen=True)
class StateDbBackupResult:
    artifact: StateDbBackupArtifact
    verified: bool
    verification_detail: str

    def __post_init__(self) -> None:
        if not isinstance(self.artifact, StateDbBackupArtifact):
            raise ValueError(
                "invalid_state_db_backup_artifact_type:"
                f"{type(self.artifact).__name__}"
            )
        if not isinstance(self.verified, bool):
            raise ValueError(
                f"invalid_verified_type:{type(self.verified).__name__}"
            )
        if (
            not isinstance(self.verification_detail, str)
            or not self.verification_detail.strip()
        ):
            raise ValueError("empty_verification_detail")
        object.__setattr__(
            self,
            "verification_detail",
            self.verification_detail.strip(),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            **self.artifact.to_manifest_dict(),
            "manifest_path": (
                str(self.artifact.manifest_path)
                if self.artifact.manifest_path is not None
                else None
            ),
            "verified": self.verified,
            "verification_detail": self.verification_detail,
        }


class StateDbBackupError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        if not isinstance(code, str) or not code.strip():
            raise ValueError("empty_state_db_backup_error_code")
        if not isinstance(message, str) or not message.strip():
            raise ValueError("empty_state_db_backup_error_message")
        self.code = code.strip()
        super().__init__(message.strip())


def default_state_db_backup_dir(source_state_db_path: Path) -> Path:
    normalized_source = _normalize_path(
        source_state_db_path,
        field_name="source_state_db_path",
    )
    return normalized_source.parent / "state-db-backups"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _format_created_at_utc(now: datetime) -> str:
    return now.astimezone(timezone.utc).replace(microsecond=0).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def _format_backup_filename_timestamp(now: datetime) -> str:
    return now.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _ensure_source_state_db_exists(path: Path) -> None:
    if not path.exists():
        raise StateDbBackupError(
            "missing_source_state_db_path",
            f"Source state DB path `{path}` does not exist.",
        )
    if path.is_dir():
        raise StateDbBackupError(
            "source_state_db_path_is_directory",
            f"Source state DB path `{path}` is a directory, not a SQLite file.",
        )


def _ensure_backup_dir(path: Path) -> None:
    try:
        if path.exists() and not path.is_dir():
            raise StateDbBackupError(
                "backup_dir_not_directory",
                f"Backup dir `{path}` exists but is not a directory.",
            )
        path.mkdir(parents=True, exist_ok=True)
    except StateDbBackupError:
        raise
    except OSError as exc:
        raise StateDbBackupError(
            "backup_dir_unusable",
            f"Backup dir `{path}` could not be created: {exc}",
        ) from exc


def _reserve_backup_paths(
    *,
    backup_dir: Path,
    filename_prefix: str,
    created_at_token: str,
    write_manifest: bool,
) -> tuple[Path, Path | None]:
    for attempt in range(100):
        suffix = "" if attempt == 0 else f"-{attempt + 1:02d}"
        stem = f"{filename_prefix}-{created_at_token}{suffix}"
        backup_path = backup_dir / f"{stem}.sqlite3"
        manifest_path = backup_dir / f"{stem}.json" if write_manifest else None
        if backup_path.exists():
            continue
        if manifest_path is not None and manifest_path.exists():
            continue
        return backup_path, manifest_path
    raise StateDbBackupError(
        "backup_target_collision",
        (
            f"Could not reserve a unique backup target in `{backup_dir}` for "
            f"timestamp `{created_at_token}`."
        ),
    )


def _connect_sqlite(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(
        path,
        timeout=_SQLITE_TIMEOUT_SECONDS,
        check_same_thread=False,
    )


def _read_schema_version_from_connection(
    connection: sqlite3.Connection,
    *,
    artifact_path: Path,
) -> int:
    try:
        row = connection.execute(
            "SELECT value FROM schema_meta WHERE key = 'schema_version'"
        ).fetchone()
    except sqlite3.DatabaseError as exc:
        raise StateDbBackupError(
            "invalid_sqlite_backup",
            f"SQLite artifact `{artifact_path}` is not readable: {exc}",
        ) from exc
    if row is None:
        raise StateDbBackupError(
            "missing_schema_version",
            f"SQLite artifact `{artifact_path}` does not contain schema_version.",
        )
    try:
        return int(row[0])
    except (TypeError, ValueError) as exc:
        raise StateDbBackupError(
            "invalid_schema_version_value",
            f"SQLite artifact `{artifact_path}` has invalid schema_version `{row[0]}`.",
        ) from exc


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_manifest(artifact: StateDbBackupArtifact) -> None:
    manifest_path = artifact.manifest_path
    if manifest_path is None:
        return
    manifest_path.write_text(
        json.dumps(
            artifact.to_manifest_dict(),
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )


def _read_manifest(path: Path) -> dict[str, object]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise StateDbBackupError(
            "backup_manifest_unreadable",
            f"Backup manifest `{path}` could not be read: {exc}",
        ) from exc
    except json.JSONDecodeError as exc:
        raise StateDbBackupError(
            "backup_manifest_invalid_json",
            f"Backup manifest `{path}` is not valid JSON: {exc}",
        ) from exc


def verify_state_db_backup(
    artifact: StateDbBackupArtifact,
) -> StateDbBackupResult:
    if not isinstance(artifact, StateDbBackupArtifact):
        raise ValueError(
            "invalid_state_db_backup_artifact_type:"
            f"{type(artifact).__name__}"
        )

    if not artifact.backup_path.exists():
        return StateDbBackupResult(
            artifact=artifact,
            verified=False,
            verification_detail=(
                f"Backup artifact `{artifact.backup_path}` does not exist."
            ),
        )
    if not artifact.backup_path.is_file():
        return StateDbBackupResult(
            artifact=artifact,
            verified=False,
            verification_detail=(
                f"Backup artifact `{artifact.backup_path}` is not a file."
            ),
        )

    actual_size_bytes = artifact.backup_path.stat().st_size
    if actual_size_bytes != artifact.size_bytes:
        return StateDbBackupResult(
            artifact=artifact,
            verified=False,
            verification_detail=(
                "Backup size mismatch: expected "
                f"{artifact.size_bytes}, got {actual_size_bytes}."
            ),
        )

    with _connect_sqlite(artifact.backup_path) as connection:
        connection.execute("PRAGMA busy_timeout=30000")
        schema_version = _read_schema_version_from_connection(
            connection,
            artifact_path=artifact.backup_path,
        )

    if schema_version != artifact.schema_version:
        return StateDbBackupResult(
            artifact=artifact,
            verified=False,
            verification_detail=(
                "Backup schema version mismatch: expected "
                f"{artifact.schema_version}, got {schema_version}."
            ),
        )

    if artifact.sha256 is not None:
        actual_sha256 = _file_sha256(artifact.backup_path)
        if actual_sha256 != artifact.sha256:
            return StateDbBackupResult(
                artifact=artifact,
                verified=False,
                verification_detail=(
                    "Backup sha256 mismatch: expected "
                    f"{artifact.sha256}, got {actual_sha256}."
                ),
            )

    if artifact.manifest_path is not None:
        if not artifact.manifest_path.exists():
            return StateDbBackupResult(
                artifact=artifact,
                verified=False,
                verification_detail=(
                    f"Backup manifest `{artifact.manifest_path}` does not exist."
                ),
            )
        manifest = _read_manifest(artifact.manifest_path)
        expected_manifest = artifact.to_manifest_dict()
        if manifest != expected_manifest:
            return StateDbBackupResult(
                artifact=artifact,
                verified=False,
                verification_detail="Backup manifest content does not match artifact metadata.",
            )

    return StateDbBackupResult(
        artifact=artifact,
        verified=True,
        verification_detail="Backup artifact verified successfully.",
    )


def create_state_db_backup(
    config: StateDbBackupConfig,
) -> StateDbBackupResult:
    if not isinstance(config, StateDbBackupConfig):
        raise ValueError(
            "invalid_state_db_backup_config_type:"
            f"{type(config).__name__}"
        )

    _ensure_source_state_db_exists(config.source_state_db_path)
    _ensure_backup_dir(config.backup_dir)

    now = _utc_now()
    created_at_utc = _format_created_at_utc(now)
    created_at_token = _format_backup_filename_timestamp(now)
    backup_path, manifest_path = _reserve_backup_paths(
        backup_dir=config.backup_dir,
        filename_prefix=config.filename_prefix,
        created_at_token=created_at_token,
        write_manifest=config.write_manifest,
    )

    try:
        with _connect_sqlite(config.source_state_db_path) as source_conn:
            source_conn.execute("PRAGMA busy_timeout=30000")
            source_schema_version = _read_schema_version_from_connection(
                source_conn,
                artifact_path=config.source_state_db_path,
            )
            with _connect_sqlite(backup_path) as backup_conn:
                source_conn.backup(backup_conn)
    except StateDbBackupError:
        raise
    except sqlite3.DatabaseError as exc:
        raise StateDbBackupError(
            "backup_copy_failed",
            (
                f"SQLite backup from `{config.source_state_db_path}` to "
                f"`{backup_path}` failed: {exc}"
            ),
        ) from exc
    except OSError as exc:
        raise StateDbBackupError(
            "backup_copy_failed",
            (
                f"SQLite backup from `{config.source_state_db_path}` to "
                f"`{backup_path}` failed: {exc}"
            ),
        ) from exc

    size_bytes = backup_path.stat().st_size
    sha256 = _file_sha256(backup_path)
    artifact = StateDbBackupArtifact(
        backup_path=backup_path,
        manifest_path=manifest_path,
        created_at_utc=created_at_utc,
        schema_version=source_schema_version,
        source_state_db_path=config.source_state_db_path,
        size_bytes=size_bytes,
        sha256=sha256,
    )
    _write_manifest(artifact)
    verification_result = verify_state_db_backup(artifact)
    if not verification_result.verified:
        raise StateDbBackupError(
            "backup_verification_failed",
            verification_result.verification_detail,
        )
    return verification_result

"""Server configuration — all settings read from environment variables."""

from __future__ import annotations

import os
from pathlib import Path
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Settings:
    """Immutable application settings derived from env vars."""

    api_key: str = field(default_factory=lambda: os.getenv("API_KEY", "changeme"))
    data_dir: Path = field(
        default_factory=lambda: Path(os.getenv("DATA_DIR", "/data"))
    )
    ctid: str = field(default_factory=lambda: os.getenv("CTID", ""))
    pwd_file_path: str = field(
        default_factory=lambda: os.getenv("PWD_FILE_PATH", "/data/pwd")
    )
    ctrader_account: str = field(
        default_factory=lambda: os.getenv("CTRADER_ACCOUNT", "")
    )
    max_parallel_jobs: int = field(
        default_factory=lambda: int(os.getenv("MAX_PARALLEL_JOBS", "2"))
    )
    max_parallel_workers_per_job: int = field(
        default_factory=lambda: int(os.getenv("MAX_PARALLEL_WORKERS_PER_JOB", "4"))
    )
    docker_image: str = field(
        default_factory=lambda: os.getenv(
            "DOCKER_IMAGE", "ghcr.io/spotware/ctrader-console:latest"
        )
    )
    pass_timeout_seconds: int = field(
        default_factory=lambda: int(os.getenv("PASS_TIMEOUT_SECONDS", "600"))
    )
    # HOST_DATA_DIR is the path to the data directory on the Docker HOST.
    # When the server runs inside a container and spawns sibling ctrader
    # containers via the Docker socket, bind-mount paths must reference
    # the host filesystem, NOT the server container's filesystem.
    # Defaults to DATA_DIR for non-Docker (local dev) setups.
    host_data_dir: Path = field(
        default_factory=lambda: Path(
            os.getenv("HOST_DATA_DIR", os.getenv("DATA_DIR", "/data"))
        )
    )

    # Derived paths (inside the server container) ----------------------------
    @property
    def algos_dir(self) -> Path:
        return self.data_dir / "algos"

    @property
    def results_dir(self) -> Path:
        return self.data_dir / "results"

    # Host-side paths (for sibling container bind mounts) --------------------
    @property
    def host_algos_dir(self) -> Path:
        return self.host_data_dir / "algos"

    @property
    def host_results_dir(self) -> Path:
        return self.host_data_dir / "results"

    @property
    def host_pwd_file_path(self) -> Path:
        """
        Host-side password file path for sibling ctrader containers.

        Keep this separate from PWD_FILE_PATH, which may refer to the
        in-container path (/data/pwd) when the server itself runs in Docker.
        """
        return Path(
            os.getenv("HOST_PWD_FILE_PATH", str(self.host_data_dir / "pwd"))
        )

    def ensure_dirs(self) -> None:
        """Create required data directories if they don't exist."""
        self.algos_dir.mkdir(parents=True, exist_ok=True)
        self.results_dir.mkdir(parents=True, exist_ok=True)


settings = Settings()

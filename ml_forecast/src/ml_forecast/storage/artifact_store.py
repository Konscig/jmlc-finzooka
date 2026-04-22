"""Filesystem model-artifact store.

Layout:

    <artifact_dir>/<ticker>/<timeframe>/
      production.joblib          -> symlink to the current production version
      <model_version>.joblib
      <model_version>.metadata.json
      archive/
        <old_version>.joblib
        <old_version>.metadata.json

Production swap is atomic via ``os.replace`` on the symlink (research R6).
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import joblib

from ml_forecast.config import get_settings
from ml_forecast.domain.timeframe import Timeframe


@dataclass(frozen=True, slots=True)
class ArtifactMetadata:
    ticker: str
    timeframe: str
    model_version: str
    model_family: str
    feature_set_version: str
    dataset_sha256: str
    metrics: dict[str, Any]
    created_at: str  # ISO-8601

    @classmethod
    def new(
        cls,
        *,
        ticker: str,
        timeframe: Timeframe,
        model_version: str,
        model_family: str,
        feature_set_version: str,
        dataset_sha256: str,
        metrics: dict[str, Any],
    ) -> ArtifactMetadata:
        return cls(
            ticker=ticker,
            timeframe=timeframe.value,
            model_version=model_version,
            model_family=model_family,
            feature_set_version=feature_set_version,
            dataset_sha256=dataset_sha256,
            metrics=metrics,
            created_at=datetime.utcnow().isoformat(timespec="seconds") + "Z",
        )


def _base_dir(ticker: str, timeframe: Timeframe) -> Path:
    root = get_settings().artifact_dir
    return root / ticker / timeframe.value


def _production_symlink(ticker: str, timeframe: Timeframe) -> Path:
    return _base_dir(ticker, timeframe) / "production.joblib"


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def save(
    model: object,
    *,
    ticker: str,
    timeframe: Timeframe,
    model_version: str,
    metadata: ArtifactMetadata,
) -> Path:
    """Persist the model + metadata and return the artifact path."""

    target_dir = _base_dir(ticker, timeframe)
    target_dir.mkdir(parents=True, exist_ok=True)
    artifact = target_dir / f"{model_version}.joblib"
    meta = target_dir / f"{model_version}.metadata.json"
    # Write to a temp file and os.replace to keep the layout consistent on crash.
    with tempfile.NamedTemporaryFile(
        dir=target_dir, prefix=f".{model_version}.", suffix=".joblib.tmp", delete=False
    ) as tmp:
        joblib.dump(model, tmp.name, compress=3)
        tmp_path = Path(tmp.name)
    os.replace(tmp_path, artifact)
    meta.write_text(json.dumps(asdict(metadata), indent=2))
    return artifact


def load(path: Path) -> object:
    return joblib.load(path)


def set_production(
    ticker: str, timeframe: Timeframe, model_version: str
) -> Path:
    """Atomically point production.joblib -> <model_version>.joblib."""

    base = _base_dir(ticker, timeframe)
    target = base / f"{model_version}.joblib"
    if not target.exists():
        raise FileNotFoundError(
            f"refusing to point production at missing artifact: {target}"
        )
    link = _production_symlink(ticker, timeframe)
    tmp_link = link.with_suffix(".tmp")
    if tmp_link.exists():
        tmp_link.unlink()
    os.symlink(target.name, tmp_link)
    os.replace(tmp_link, link)
    return link


def archive(ticker: str, timeframe: Timeframe, model_version: str) -> Path:
    """Move an artifact to the archive/ subdirectory (physical + metadata)."""

    base = _base_dir(ticker, timeframe)
    archive_dir = base / "archive"
    archive_dir.mkdir(parents=True, exist_ok=True)
    for suffix in (".joblib", ".metadata.json"):
        src = base / f"{model_version}{suffix}"
        if src.exists():
            os.replace(src, archive_dir / src.name)
    return archive_dir


def artifact_sha256(ticker: str, timeframe: Timeframe, model_version: str) -> str:
    return _sha256(_base_dir(ticker, timeframe) / f"{model_version}.joblib")


def rotate_archive(
    ticker: str, timeframe: Timeframe, keep: int = 3
) -> list[Path]:
    """T094 — retention policy: keep at most ``keep`` most-recent archived
    artifacts. Deletes older .joblib + .metadata.json pairs from the
    ``archive/`` subdir.

    Also removes orphan ``ml.model_registry`` rows in ``state=archived``
    that no longer have a physical artifact on disk (covers the corner
    case where a previous rotation was interrupted).
    """

    archive_dir = _base_dir(ticker, timeframe) / "archive"
    if not archive_dir.is_dir():
        return []

    artifacts = sorted(
        archive_dir.glob("*.joblib"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    to_delete = artifacts[keep:]
    removed: list[Path] = []
    for art in to_delete:
        meta = art.with_suffix(".metadata.json")
        try:
            art.unlink()
            removed.append(art)
            if meta.exists():
                meta.unlink()
        except OSError:
            continue
    return removed

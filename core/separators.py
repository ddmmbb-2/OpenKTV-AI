from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .config import AppSettings
from .demucs_separator import separate_with_demucs
from .uvr_separator import separate_with_uvr


@dataclass(frozen=True)
class SeparationArtifacts:
    vocals_path: Path
    accompaniment_path: Path
    backend_used: str


def separate_audio(
    input_path: Path,
    work_dir: Path,
    settings: AppSettings,
    stems: int,
    device_preference: str,
    backend: str,
    log_cb=print,
) -> SeparationArtifacts:
    selected = (backend or settings.separator_backend or "demucs").strip().lower()
    if selected in {"none", ""}:
        selected = (settings.separator_backend or "demucs").strip().lower()
    if selected not in {"demucs", "uvr", "hybrid"}:
        selected = "demucs"
    if selected == "demucs":
        vocals, accompaniment = separate_with_demucs(
            input_path,
            work_dir / "demucs",
            settings.demucs_model,
            stems,
            device_preference,
            log_cb=log_cb,
        )
        return SeparationArtifacts(vocals, accompaniment, "demucs")

    if selected == "uvr":
        vocals, accompaniment = separate_with_uvr(
            input_path,
            work_dir / "uvr",
            settings.uvr_model_dir,
            settings.uvr_model,
            log_cb=log_cb,
        )
        return SeparationArtifacts(vocals, accompaniment, "uvr")

    if selected == "hybrid":
        demucs_vocals, accompaniment = separate_with_demucs(
            input_path,
            work_dir / "demucs",
            settings.demucs_model,
            stems,
            device_preference,
            log_cb=log_cb,
        )
        del demucs_vocals
        vocals, _uvr_instrumental = separate_with_uvr(
            input_path,
            work_dir / "uvr",
            settings.uvr_model_dir,
            settings.uvr_model,
            log_cb=log_cb,
        )
        return SeparationArtifacts(vocals, accompaniment, "hybrid")

    raise ValueError(f"不支援的分離後端: {selected}")

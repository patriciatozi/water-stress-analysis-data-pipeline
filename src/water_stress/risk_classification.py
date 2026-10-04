from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from math import isfinite


class RiskClass(StrEnum):
    LOW = "low"
    ATTENTION = "attention"
    HIGH = "high"
    CRITICAL = "critical"


@dataclass(frozen=True)
class RiskBand:
    upper_score: float
    risk_class: RiskClass
    label: str
    monitoring_guidance: str


@dataclass(frozen=True)
class RiskClassificationPolicy:
    version: str
    bands: tuple[RiskBand, ...]
    missing_score_guidance: str


RISK_CLASSIFICATION_POLICY = RiskClassificationPolicy(
    version="four-level-v1",
    bands=(
        RiskBand(0.25, RiskClass.LOW, "Baixo", "Manter monitoramento de rotina."),
        RiskBand(0.50, RiskClass.ATTENTION, "Atenção", "Monitorar a tendência do indicador."),
        RiskBand(0.75, RiskClass.HIGH, "Alto", "Priorizar avaliação das condições da área."),
        RiskBand(1.00, RiskClass.CRITICAL, "Crítico", "Avaliar as condições da área com urgência."),
    ),
    missing_score_guidance="Sem dados suficientes para classificar o risco.",
)


def classify_score(score: float | None) -> RiskBand | None:
    """Classify the unrounded academic index; upper bounds are inclusive.

    A missing score has no risk class. The bands are provisional interpretation
    rules, independent of the score formula and of any irrigation decision.
    """
    if score is None:
        return None
    if not isfinite(score) or not 0 <= score <= 1:
        raise ValueError("Water-stress score must be finite and between 0 and 1, or None")
    return next(band for band in RISK_CLASSIFICATION_POLICY.bands if score <= band.upper_score)


def lower_index_stress(value: float, threshold: float) -> float:
    """Shared v1/v2 normalization; lower spectral indices increase the component."""
    return max(0.0, min(1.0, (threshold - value) / max(abs(threshold), 1e-9)))

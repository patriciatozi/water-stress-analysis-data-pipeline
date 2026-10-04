from math import nextafter

import pytest

from water_stress.risk_classification import classify_score


@pytest.mark.parametrize(
    ("score", "risk_class", "label", "guidance"),
    [
        (0.0, "low", "Baixo", "Manter monitoramento de rotina."),
        (0.25, "low", "Baixo", "Manter monitoramento de rotina."),
        (
            nextafter(0.25, 1.0),
            "attention",
            "Atenção",
            "Monitorar a tendência do indicador.",
        ),
        (0.257, "attention", "Atenção", "Monitorar a tendência do indicador."),
        (0.50, "attention", "Atenção", "Monitorar a tendência do indicador."),
        (
            nextafter(0.50, 1.0),
            "high",
            "Alto",
            "Priorizar avaliação das condições da área.",
        ),
        (0.75, "high", "Alto", "Priorizar avaliação das condições da área."),
        (
            nextafter(0.75, 1.0),
            "critical",
            "Crítico",
            "Avaliar as condições da área com urgência.",
        ),
        (1.0, "critical", "Crítico", "Avaliar as condições da área com urgência."),
    ],
)
def test_classifies_unrounded_score(
    score: float, risk_class: str, label: str, guidance: str
) -> None:
    band = classify_score(score)
    assert band is not None
    assert band.risk_class == risk_class
    assert band.label == label
    assert band.monitoring_guidance == guidance


def test_missing_score_has_no_risk_class() -> None:
    assert classify_score(None) is None


@pytest.mark.parametrize("score", [-0.01, 1.01, float("nan"), float("inf"), -float("inf")])
def test_rejects_invalid_score(score: float) -> None:
    with pytest.raises(ValueError, match="finite and between 0 and 1"):
        classify_score(score)

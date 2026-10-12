"""Run with uv run --group dashboard streamlit run dashboard_app.py."""

from __future__ import annotations

import argparse
import base64
import logging
from datetime import date
from html import escape
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import pyarrow as pa
import pydeck as pdk
import streamlit as st
from dotenv import load_dotenv
from psycopg import Error as DatabaseError

from water_stress.config import Settings, load_settings
from water_stress.dashboard.analysis import (
    RISK_COLORS,
    RISK_LABELS,
    STATUS_LABELS,
    GeographicBounds,
    WeeklySummary,
    geographic_subset,
    risk_keys,
    summarize,
    summarize_months,
)
from water_stress.dashboard.data import DashboardReader, DataSource
from water_stress.dashboard.s3_cache import DashboardStorageSettings, fetch_dashboard
from water_stress.risk_classification import RISK_CLASSIFICATION_POLICY
from water_stress.transformation import gold_weekly

LOGGER = logging.getLogger(__name__)
PAGE_TITLE = "Riscos hídricos para o cultivo da soja"
PROJECT_NAME = "Estresse Hídrico e Recomendação de Irrigação na Soja"
COURSE_LOGO = Path(__file__).resolve().parent / "assets" / "pece-logo.png"
MONTH_NAMES = ("jan", "fev", "mar", "abr", "mai", "jun", "jul", "ago", "set", "out", "nov", "dez")
MAP_RISK_LABELS = {
    key: label for key, label in RISK_LABELS.items() if key not in ("pending", "missing")
}
MAP_CATEGORY_LABELS = {
    **MAP_RISK_LABELS,
    **{key: STATUS_LABELS[key] for key in ("complete", "partial")},
}

FACTOR_LABELS = {
    "precipitation_mm_7d": "Chuva",
    "eto_mm_7d": "Demanda de água do ar (ETo)",
    "water_deficit_mm_7d": "Falta de chuva frente à demanda",
    "ndvi_median": "Vigor da vegetação (NDVI)",
    "ndmi_median": "Umidade da vegetação (NDMI)",
    "temperature_mean_c": "Temperatura média",
    "consecutive_dry_days": "Sequência seca",
    "clay_pct": "Argila",
    "sand_pct": "Areia",
    "soc": "Carbono orgânico",
    "bulk_density": "Densidade do solo",
}
CSS = """
<style>
.block-container {max-width: 1480px; padding-top: 2rem; padding-bottom: 2rem;}
h1 {letter-spacing: -.045em; font-weight: 650 !important;}
h1, h2, h3 {color: var(--dashboard-primary);}
[data-testid="stSidebar"] :is(h1, h2, h3) {color: inherit;}
h2, h3 {letter-spacing: -.025em;}
[data-testid="stMetric"] {background: white; border: 1px solid #dce5ef;
    border-radius: 14px; padding: 18px 20px; min-height: 130px;}
[data-testid="stMetricValue"] {font-size: 2rem; letter-spacing: -.03em;}
[data-testid="stSidebar"] {border-right: 1px solid #dce5ef;}
.sidebar-brand {padding-bottom: 24px; margin-bottom: 6px;
    border-bottom: 1px solid #ffffff30;}
.sidebar-brand img {display: block; width: 224px; max-width: 100%; height: auto;
    margin-bottom: 24px;}
.sidebar-brand h2 {font-size: 1.15rem !important; line-height: 1.45;
    font-weight: 600 !important; letter-spacing: -.01em; margin: 0; padding: 0;}
div[data-testid="stVerticalBlockBorderWrapper"] > div {border-radius: 14px;}
.st-key-map_categories :is([data-tag], [data-baseweb="tag"]) {
    background-color: var(--dashboard-primary) !important; color: white !important;}
.st-key-map_categories :is([data-tag], [data-baseweb="tag"]):has([title="Completo"]),
.st-key-map_categories :is([data-tag], [data-baseweb="tag"]):has([title="Parcial"]) {
    background-color: #f4d46f !important; color: #594514 !important;}
.st-key-map_categories :is([data-tag], [data-baseweb="tag"]) button {color: inherit !important;}
.score-description {background: var(--dashboard-soft); color: var(--dashboard-text);
    border-radius: 8px;
    padding: 16px; margin-bottom: 0; font-size: 1rem; line-height: 1.6;}
.score-description p {margin: 16px 0 0;}
.score-term {position: relative; display: inline-block;
    border-bottom: 1px dotted currentColor; cursor: help;}
.score-term:focus-visible {outline: 2px solid currentColor; outline-offset: 3px;}
.score-term [role="tooltip"] {visibility: hidden; opacity: 0; position: absolute;
    bottom: calc(100% + 8px); left: 0; z-index: 100;
    width: 260px; max-width: 75vw; padding: 10px 12px; border-radius: 8px;
    background: var(--dashboard-text); color: white; font-size: .875rem; line-height: 1.4;
    font-weight: normal; box-shadow: 0 4px 12px #23354826; pointer-events: none;}
.score-term:hover [role="tooltip"], .score-term:focus [role="tooltip"] {
    visibility: visible; opacity: 1;}
#eto-description {left: auto; right: 0;}
.risk-scale {display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 12px;}
.risk-band {background: white; border: 1px solid #dce5ef; border-top: 4px solid;
    border-radius: 8px; padding: 10px 14px; color: var(--dashboard-text);}
.risk-band p {margin: 4px 0 0; font-size: .95rem;}
@media (max-width: 700px) {
    .risk-scale {grid-template-columns: repeat(2, minmax(0, 1fr));}
}
@media (max-width: 1400px) {
    .score-term [role="tooltip"], #eto-description {position: fixed;
        bottom: 24px; left: 50%; right: auto; transform: translateX(-50%);}
}
</style>
"""


def render_sidebar_brand() -> None:
    logo = base64.b64encode(COURSE_LOGO.read_bytes()).decode("ascii")
    st.html(
        '<div class="sidebar-brand">'
        f'<img src="data:image/png;base64,{logo}" '
        'alt="PECE - Programa de Educação Continuada, Escola Politécnica da USP" '
        'width="224" height="91">'
        f"<h2>{PROJECT_NAME}</h2>"
        "</div>"
    )


def number(value: float | None, decimals: int = 1, suffix: str = "") -> str:
    if value is None or pd.isna(value):
        return "-"
    formatted = f"{value:,.{decimals}f}".replace(",", "_").replace(".", ",").replace("_", ".")
    return f"{formatted}{suffix}"


def month_label(value: date) -> str:
    return f"{MONTH_NAMES[value.month - 1]}/{value.year}"


@st.cache_data(ttl=300, max_entries=2, show_spinner=False)
def cached_grid(_settings: Settings, identity: str) -> pa.Table:
    return DashboardReader(_settings).spatial_grid()


@st.cache_data(ttl=60, max_entries=2, show_spinner=False)
def cached_s3_settings(
    _settings: Settings, config: DashboardStorageSettings, identity: str
) -> Settings:
    return fetch_dashboard(_settings, config)


@st.cache_data(ttl=300, max_entries=3, show_spinner=False)
def cached_week(_settings: Settings, source: DataSource, identity: str, start: date) -> pa.Table:
    reader = DashboardReader(_settings, source)
    grid = cached_grid(_settings, identity) if source == "parquet" else None
    return reader.week(start, grid)


@st.cache_data(ttl=300, max_entries=3, show_spinner=False)
def cached_history(
    _settings: Settings, source: DataSource, identity: str, grid_id: str
) -> pa.Table:
    return DashboardReader(_settings, source).history(grid_id)


@st.cache_data(ttl=300, max_entries=2, show_spinner=False)
def cached_evolution(
    _settings: Settings, source: DataSource, identity: str, bounds: GeographicBounds | None
) -> pa.Table:
    reader = DashboardReader(_settings, source)
    grid = cached_grid(_settings, identity) if source == "parquet" else None
    return reader.evolution(grid, bounds)


def style_chart(figure: go.Figure, height: int = 280, y_title: str | None = None) -> go.Figure:
    figure.update_layout(
        template="plotly_white",
        height=height,
        margin=dict(l=0, r=8, t=12, b=0),
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        font=dict(family="Arial, sans-serif", size=12, color="#526881"),
        legend=dict(orientation="h", y=-0.2),
        hovermode="x unified",
        yaxis_title=y_title,
        xaxis_title=None,
    )
    figure.update_yaxes(gridcolor="#e8edf1", zeroline=False)
    return figure


def line_chart(
    data: pd.DataFrame,
    columns: dict[str, str],
    *,
    scale: float = 1,
    y_title: str | None = None,
    colors: tuple[str, ...] | None = None,
) -> go.Figure:
    colors = colors or (st.get_option("theme.primaryColor"), "#558bb8", "#dba44a")
    figure = go.Figure()
    for color, (column, label) in zip(colors, columns.items(), strict=False):
        figure.add_trace(
            go.Scatter(
                x=data["week_start"],
                y=data[column] * scale,
                name=label,
                mode="lines",
                line=dict(color=color, width=2.5),
                connectgaps=False,
            )
        )
    return style_chart(figure, y_title=y_title)


def period_data(table: pa.Table, settings: Settings) -> pd.DataFrame:
    """Insert plotting gaps for unpublished weeks; do not fill factor/score values."""
    weeks = [start for start, _ in gold_weekly._study_weeks(settings)]
    return table.to_pandas().set_index("week_start").reindex(weeks).reset_index()


def render_metrics(summary: WeeklySummary) -> None:
    score, high, complete = st.columns(3)
    score.metric(
        "Indicador médio - 0 a 100",
        number(None if summary.score is None else summary.score * 100),
        help=(
            "Média ponderada pela área de soja com score. Pode combinar completos e parciais; "
            "consulte a composição abaixo."
        ),
    )
    high.metric(
        "Área com sinais altos ou críticos",
        number(
            summary.high_risk_area_km2
            if summary.scored_area_km2 > summary.pending_class_area_km2
            else None,
            0,
            " km²",
        ),
        help=(
            "Soma da área equivalente de soja nas classes de alto e crítico "
            "para o estresse hídrico. "
            "Não é área com dano observado."
        ),
    )
    complete.metric(
        "Área com os três componentes",
        number(summary.complete_coverage_pct, suffix="%"),
        help=(
            "Área equivalente com todos os componentes / área equivalente total de soja no recorte."
        ),
    )


def open_indicator_method() -> None:
    st.session_state["section"] = "Sobre o indicador"


def render_score_description() -> None:
    st.html("""
        <div class="score-description">
            <strong>O que este indicador mostra?</strong>
            <p>Reúne fatores relacionados ao clima e de imagens de satélite para compor um
            <b>índice de estresse hídrico com escala de 0 a 100</b>.<br>
            <b>Maiores valores</b> indicam condições mais associadas à <b>falta de água</b>
            naquele determinado período e região.
        </div>
    """)
    with st.expander("Como o indicador é composto?"):
        st.html("""
            <p>Combina a falta de chuva frente à <b>demanda atmosférica de água</b>
            (<span class="score-term" tabindex="0" aria-describedby="eto-description">ETo<span
                role="tooltip" id="eto-description">Água que uma vegetação de referência
                perderia para o ar por evaporação e transpiração.</span></span>),
            o <b>vigor da vegetação</b>
            (<span class="score-term" tabindex="0" aria-describedby="ndvi-description">NDVI<span
                role="tooltip" id="ndvi-description">Indica o vigor e a cobertura da vegetação
                a partir de imagens de satélite.</span></span>)
            e sua <b>umidade</b>
            (<span class="score-term" tabindex="0" aria-describedby="ndmi-description">NDMI<span
                role="tooltip" id="ndmi-description">Indica a umidade da vegetação a partir
                de imagens de satélite.</span></span>);
            resultados parciais usam os fatores disponíveis.</p>
        """)
        st.button(
            "Ver detalhes na seção **Sobre o indicador**",
            key="open_indicator_method",
            on_click=open_indicator_method,
        )


def render_risk_scale() -> None:
    st.subheader("Como ler as faixas de risco?")
    cards = []
    lower_score = 0.0
    for index, band in enumerate(RISK_CLASSIFICATION_POLICY.bands):
        upper_score = band.upper_score * 100
        interval = (
            f"0 a {upper_score:g}"
            if index == 0
            else f"Acima de {lower_score:g} até {upper_score:g}"
        )
        color = RISK_COLORS[band.risk_class.value]
        cards.append(
            f'<div class="risk-band" role="listitem" style="border-top-color: {color};">'
            f"<strong>{escape(band.label)}</strong><p>{interval}</p></div>"
        )
        lower_score = upper_score
    st.html(
        '<div class="risk-scale" role="list" aria-label="Faixas do indicador na escala de 0 a 100">'
        + "".join(cards)
        + "</div>"
    )

def render_map(table: pa.Table, summary: WeeklySummary) -> None:
    left, right = st.columns([2.15, 1], gap="large")
    with left, st.container(border=True):
        st.subheader("Onde estão os sinais de risco nesta semana?")
        chosen_categories = st.multiselect(
            "Categorias",
            list(MAP_CATEGORY_LABELS),
            default=list(MAP_RISK_LABELS),
            format_func=MAP_CATEGORY_LABELS.get,
            key="map_categories",
            help=(
                "Classes em azul; completude em amarelo. As classes escolhidas são combinadas "
                "com Completo/Parcial quando selecionados. Sem filtro de completude, "
                "todas as condições são exibidas."
            ),
        )
        chosen_classes = [key for key in chosen_categories if key in MAP_RISK_LABELS]
        chosen_statuses = [key for key in chosen_categories if key in ("complete", "partial")]
        data = table.select(
            [
                "grid_id",
                "centroid_latitude",
                "centroid_longitude",
                "water_stress_score",
                "score_status",
                "soy_area_km2",
            ]
        ).to_pandas()
        data["risk_key"] = risk_keys(table)
        data = data[data["risk_key"].isin(MAP_RISK_LABELS)]
        if not chosen_categories:
            data = data.iloc[:0]
        if chosen_classes:
            data = data[data["risk_key"].isin(chosen_classes)]
        if chosen_statuses:
            data = data[data["score_status"].isin(chosen_statuses)]
        data = data.copy()
        risk_area = {
            key: float(data.loc[data["risk_key"] == key, "soy_area_km2"].sum())
            for key in MAP_RISK_LABELS
        }
        if data.empty:
            st.info("Nenhuma célula nas categorias selecionadas.")
        else:
            data["color"] = data["risk_key"].map(
                lambda key: [*bytes.fromhex(RISK_COLORS[key][1:]), 210]
            )
            data["risk_label"] = data["risk_key"].map(MAP_RISK_LABELS)
            data["score_display"] = data["water_stress_score"].map(
                lambda value: number(value * 100)
            )
            data["status_label"] = data["score_status"].map(
                lambda value: STATUS_LABELS.get(value, "Status pendente")
            )
            layer = pdk.Layer(
                "ScatterplotLayer",
                id="soy-cells",
                data=data,
                get_position="[centroid_longitude, centroid_latitude]",
                get_fill_color="color",
                get_radius=300,
                radius_min_pixels=2,
                radius_max_pixels=7,
                pickable=True,
                auto_highlight=True,
            )
            view = pdk.ViewState(
                latitude=float(data["centroid_latitude"].mean()),
                longitude=float(data["centroid_longitude"].mean()),
                zoom=5.3,
                pitch=0,
            )
            deck = pdk.Deck(
                layers=[layer],
                initial_view_state=view,
                map_style="light",
                map_provider="carto",
                tooltip={
                    "text": "{grid_id}\nScore: {score_display}\n{risk_label} · {status_label}"
                },
            )
            st.pydeck_chart(deck, height=440)
            st.caption(
                "Marcadores são centróides da grade de 1 km;"
                " não representam o contorno dos talhões."
            )
    with right:
        with st.container(border=True):
            st.subheader("Como a área se distribui?")
            keys = list(MAP_RISK_LABELS)
            figure = go.Figure(
                go.Bar(
                    x=[risk_area[key] for key in keys],
                    y=[MAP_RISK_LABELS[key] for key in keys],
                    orientation="h",
                    marker_color=[RISK_COLORS[key] for key in keys],
                    text=[number(risk_area[key], 1, " km²") for key in keys],
                    textposition="auto",
                )
            )
            figure.update_yaxes(autorange="reversed")
            st.plotly_chart(
                style_chart(figure, height=260), width="stretch", config={"displayModeBar": False}
            )
            st.caption("Área equivalente de soja, em km², nas categorias selecionadas no mapa.")
        with st.container(border=True):
            st.subheader("Quais dados estão disponíveis?")
            st.caption(
                "Área de soja com os 3 fatores do score (completo) ou parte deles (parcial)."
            )
            areas = {
                "Completo": summary.complete_area_km2,
                "Parcial": summary.partial_area_km2,
            }
            for label, area in areas.items():
                st.write(f"**{label}** - {number(area, 0)} km²")
            st.write(
                "**Sem indicador** - "
                f"{number(summary.soy_area_km2 - summary.scored_area_km2, 0)} km²"
            )
            if summary.pending_class_area_km2:
                st.caption(
                    f"{number(summary.pending_class_area_km2, 1)} km² têm indicador, "
                    "mas ainda não têm classe publicada."
                )


def render_factors(summary: WeeklySummary) -> None:
    st.subheader("Fatores da semana")
    climate, vegetation = st.columns(2, gap="large")
    with climate, st.container(border=True):
        st.markdown("**Disponibilidade e demanda atmosférica**")
        columns = ["precipitation_mm_7d", "eto_mm_7d", "water_deficit_mm_7d"]
        figure = go.Figure(
            go.Bar(
                x=[FACTOR_LABELS[name] for name in columns],
                y=[summary.factors[name] for name in columns],
                marker_color=["#558bb8", "#8cabc9", "#dba44a"],
            )
        )
        st.plotly_chart(
            style_chart(figure, height=220, y_title="mm na janela"),
            width="stretch",
            config={"displayModeBar": False},
        )
        st.caption(
            "Déficit = média dos déficits das células; "
            "não é a diferença entre as duas médias do gráfico."
        )
        st.caption(
            f"Área com dados: chuva {number(summary.factor_coverage['precipitation_mm_7d'])}% · "
            f"demanda {number(summary.factor_coverage['eto_mm_7d'])}% · "
            f"déficit {number(summary.factor_coverage['water_deficit_mm_7d'])}%"
        )
    with vegetation, st.container(border=True):
        st.markdown("**Condição da vegetação e umidade espectral**")
        first, second = st.columns(2)
        first.metric("Vigor (NDVI)", number(summary.factors["ndvi_median"], 3))
        second.metric("Umidade (NDMI)", number(summary.factors["ndmi_median"], 3))
        st.caption(
            "NDVI e NDMI são médias ponderadas das medianas Gold disponíveis; faixa de -1 a 1."
        )
        st.caption(
            f"Cobertura NDVI: {number(summary.factor_coverage['ndvi_median'])}% · "
            f"NDMI: {number(summary.factor_coverage['ndmi_median'])}%"
        )
        st.write(f"Temperatura média: **{number(summary.factors['temperature_mean_c'])} °C**")


def render_period(
    settings: Settings, source: DataSource, identity: str, bounds: GeographicBounds | None
) -> None:
    st.subheader("Como os sinais de risco mudaram ao longo do período?")
    with st.spinner("Resumindo todas as semanas publicadas…"):
        evolution = cached_evolution(settings, source, identity, bounds)
    months = summarize_months(evolution, settings.study.start_date, settings.study.end_date)
    observed = [month for month in months if month.score is not None]
    if not observed:
        st.info(
            "Não há indicador disponível no período. Ausência de dados não significa baixo risco."
        )
    else:
        peak = max(observed, key=lambda month: month.score if month.score is not None else -1)
        peak_score = peak.score * 100 if peak.score is not None else None
        ties = [month for month in observed if month.score == peak.score]
        if len(observed) == 1:
            st.write(
                f"**{month_label(peak.month)} é o único mês com indicador disponível "
                f"({number(peak_score)} pontos).** Ainda não é possível comparar meses."
            )
        elif len(ties) == len(observed):
            st.write(f"**Os meses com dados têm a mesma média: {number(peak_score)} pontos.**")
        else:
            labels = ", ".join(month_label(month.month) for month in ties)
            st.write(f"Maior indicador médio em {labels}: **{number(peak_score)} pontos.**")
    render_score_description()
    labels = [month_label(month.month) for month in months]
    left, right = st.columns([1.6, 1], gap="large")
    with left, st.container(border=True):
        st.markdown("**Em quais meses os sinais foram mais fortes?**")
        figure = go.Figure(
            go.Scatter(
                x=labels,
                y=[None if month.score is None else month.score * 100 for month in months],
                mode="lines+markers+text",
                text=[
                    number(None if month.score is None else month.score * 100) for month in months
                ],
                textposition="top center",
                line=dict(color="#dba44a", width=3),
                marker=dict(size=8),
                connectgaps=False,
                hovertemplate="%{x}: %{y:.1f} pontos<extra></extra>",
            )
        )
        figure.update_yaxes(range=[0, 110], tickvals=[0, 25, 50, 75, 100])
        st.plotly_chart(
            style_chart(figure, height=310, y_title="Indicador médio - 0 a 100"),
            width="stretch",
            config={"displayModeBar": False},
        )
        st.caption(
            "Média dos resultados semanais, ponderada pela **área de soja** "
            "e pelos **dias no mês**."
        )
    with right, st.container(border=True):
        st.markdown("**Quanto da área tem os três componentes?**")
        figure = go.Figure(
            go.Bar(
                x=labels,
                y=[month.complete_coverage_pct for month in months],
                marker_color="#112B49",
                text=[number(month.complete_coverage_pct, 0, "%") for month in months],
                textposition="outside",
                cliponaxis=False,
                hovertemplate="%{x}: %{y:.1f}% da área observada<extra></extra>",
            )
        )
        figure.update_yaxes(range=[0, 110], tickvals=[0, 25, 50, 75, 100])
        st.plotly_chart(
            style_chart(figure, height=310, y_title="% da área observada"),
            width="stretch",
            config={"displayModeBar": False},
        )
        st.caption("**Três componentes:** clima, vigor e umidade da vegetação.")
    if any(month.available_days < month.expected_days for month in months):
        st.info(
            "Há meses com semanas ausentes ou sem células. As médias usam apenas os dias "
            "representados; confira a disponibilidade na tabela abaixo."
        )
    with st.expander("Comparar os meses em uma tabela"):
        st.dataframe(
            pd.DataFrame(
                [
                    {
                        "Mês": month_label(month.month),
                        "Indicador médio (0-100)": None
                        if month.score is None
                        else month.score * 100,
                        "Área em alto/crítico entre as classificadas (%)": month.high_risk_pct,
                        "Área com indicador (%)": month.scored_coverage_pct,
                        "Área com classificação (%)": month.classified_coverage_pct,
                        "Área com três componentes (%)": month.complete_coverage_pct,
                        "Dias representados / dias do estudo": (
                            f"{month.available_days}/{month.expected_days}"
                        ),
                    }
                    for month in months
                ]
            ),
            hide_index=True,
            width="stretch",
        )
        st.caption(
            "Percentuais são **médias ponderadas por área e dias nas semanas disponíveis**.<br>"
            "Alto/crítico usa somente a **área com classe publicada**.",
            unsafe_allow_html=True,
        )
    with st.expander("Observações importantes"):
        st.caption(
            "Lacunas representam **ausência de dados**. <br>"
            "Os meses são **resumos de semanas**, não medições diárias "
            "nem novas classificações de risco.",
            unsafe_allow_html=True,
        )
        st.caption("A identificação de soja é **anual** e não confirma cultivo ativo em cada mês.")


def render_cell(settings: Settings, source: DataSource, identity: str, week: date) -> None:
    st.subheader("Histórico da célula")
    st.caption("Informe o código mostrado ao passar o cursor sobre um marcador no mapa.")
    grid_id = st.text_input(
        "Código da célula",
        placeholder=f"{settings.study.area_code}_{settings.spatial.screening_grid_meters}m_r…_c…",
    ).strip()
    if not grid_id:
        st.info("Informe uma célula para ver sua evolução e os fatores associados.")
        return
    with st.spinner("Lendo o histórico da célula…"):
        table = cached_history(settings, source, identity, grid_id)
    if not table.num_rows:
        st.info("Esta célula não está nos dados Gold v1 disponíveis para o estudo.")
        return
    observed = table.to_pandas()
    data = period_data(table, settings)
    selected = observed[observed["week_start"] == week]
    if not selected.empty:
        row = selected.iloc[0]
        status = STATUS_LABELS.get(row["score_status"], "Status pendente")
        risk = (
            "Sem score"
            if pd.isna(row["water_stress_score"])
            else RISK_LABELS.get(row["water_stress_risk_class"], "Classificação pendente")
        )
        st.write(f"**{risk} · {status}** - score {number(row['water_stress_score'] * 100)}")
        age = number(row["satellite_age_days"], 0)
        available_weight = number(row["score_available_weight"] * 100)
        days = (
            f"{number(row['weather_observation_count'], 0)}/"
            f"{number(row['weather_expected_days'], 0)}"
        )
        st.caption(
            f"Componentes disponíveis: {number(row['score_component_count'], 0)} · "
            f"peso disponível: {available_weight}% · idade do satélite: {age} dias · "
            f"dias de clima: {days}"
        )
        if pd.notna(row["monitoring_guidance"]):
            st.info(row["monitoring_guidance"])
        st.caption(
            f"Solo 0-30 cm · argila {number(row['clay_pct'])}% · "
            f"areia {number(row['sand_pct'])}% · "
            f"carbono orgânico {number(row['soc'])} g/kg · "
            f"densidade {number(row['bulk_density'], 2)} g/cm³. "
            "Solo é contexto; não compõe o score v1."
        )
    left, right = st.columns(2, gap="large")
    with left, st.container(border=True):
        st.plotly_chart(
            line_chart(
                data, {"water_stress_score": "Score da célula"}, scale=100, y_title="Score · 0-100"
            ),
            width="stretch",
        )
    with right, st.container(border=True):
        st.plotly_chart(
            line_chart(
                data,
                {
                    "precipitation_mm_7d": "Chuva",
                    "eto_mm_7d": "ETo",
                    "water_deficit_mm_7d": "Déficit",
                },
                y_title="mm na janela",
                colors=("#558bb8", "#8cabc9", "#dba44a"),
            ),
            width="stretch",
        )
    st.plotly_chart(
        line_chart(data, {"ndvi_median": "NDVI", "ndmi_median": "NDMI"}, y_title="Índice"),
        width="stretch",
    )
    st.download_button(
        "Baixar histórico da célula",
        observed.to_csv(index=False).encode("utf-8-sig"),
        file_name="historico_celula.csv",
        mime="text/csv",
    )


def render_method(reader: DashboardReader) -> None:
    st.subheader("Como ler este indicador")
    context = reader.context()
    st.write(
        "O indicador v1 reúne a falta de chuva frente à demanda de água do ar (ETo), "
        "o vigor da vegetação (NDVI) e sua umidade (NDMI). É um indicador acadêmico "
        "provisório de condições associadas à falta de água na soja."
    )
    st.write(
        "A máscara de soja é anual. Uma célula não necessariamente tem soja ativa em todas as "
        "semanas. ETo expressa demanda atmosférica de referência; não é ETc da soja. "
        "Retenção de água no solo e o novo balanço v2 não entram neste dashboard."
    )
    st.write(
        "Os índices ausentes são retirados da composição, e os pesos disponíveis são "
        "renormalizados. Sem clima completo, o score fica indisponível. Score zero é válido. "
        "Os valores nulos permanecem ausentes nos gráficos."
    )
    render_risk_scale()
    st.write(
        "O mapa e a área por classe exibem somente células com classificação publicada. "
        "Os demais indicadores usam os valores numéricos disponíveis no período."
    )
    st.write(
        "Área equivalente de soja = área da célula * fração de soja. Resumos usam essa área, "
        "sem tratar células parcialmente cultivadas como 1 km² integral de soja. "
        "Cada fator usa somente a área com valor disponível."
    )
    st.caption(f"Método: {context['method']} · parâmetros: {context['parameters_origin']}")
    parameters = context["parameters"]
    with st.expander("Consultar os parâmetros técnicos"):
        st.dataframe(
            pd.DataFrame(
                [{"Parâmetro": key, "Valor": str(value)} for key, value in parameters.items()]
            ),
            hide_index=True,
            width="stretch",
        )
    if reader.source == "postgres":
        st.caption(
            "Os parâmetros acima são os configurados atualmente; o banco não armazena todos "
            "os pesos e limiares da execução que gerou cada linha. "
            "Confira a linhagem da carga para comparações."
        )
    elif context["processed_at"]:
        st.caption(f"Processamento Gold: {context['processed_at']}")
    st.caption(
        "Meteorologia NASA POWER tem resolução mais grossa que a grade de 1 km. "
        "Índices espectrais não são medições diretas da água do solo. "
        "As orientações não prescrevem irrigação."
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=Path("configs/project.yml"))
    args = parser.parse_args()
    load_dotenv(Path(".env"), override=False)
    st.set_page_config(page_title=PAGE_TITLE, page_icon="🌿", layout="wide")
    st.html(
        "<style>:root {"
        f"--dashboard-primary: {st.get_option('theme.primaryColor')};"
        f"--dashboard-soft: {st.get_option('theme.secondaryBackgroundColor')};"
        f"--dashboard-text: {st.get_option('theme.textColor')};"
        "}</style>" + CSS
    )
    settings = load_settings(args.config)
    st.title(PAGE_TITLE)
    st.caption(
        f"**Período:** {settings.study.start_date:%d/%m/%Y} a "
        f"{settings.study.end_date:%d/%m/%Y}<br>"
        f"**Região:** Estado do **{escape(settings.study.area_name)}**",
        unsafe_allow_html=True,
    )
    with st.sidebar:
        render_sidebar_brand()
        st.subheader("Explorar os dados")
    source: DataSource = "postgres"
    storage: DashboardStorageSettings | None = None
    try:
        storage = DashboardStorageSettings()
        if storage.source == "s3":
            with st.spinner("Lendo a publicação do S3…"):
                settings = cached_s3_settings(
                    settings, storage, str(settings.study.model_dump()) + settings.config_hash
                )
            source = "parquet"
        reader = DashboardReader(settings, source)
        weeks = reader.weeks()
        identity = reader.fingerprint() + str(settings.study.model_dump()) + settings.config_hash
        with st.sidebar:
            section = st.radio(
                "Visualização",
                ["Visão geral", "Explorar por semana", "Sobre o indicador"],
                key="section",
            )
            bounds = None
            week = None
            if section == "Explorar por semana":
                week = st.select_slider(
                    "Semana",
                    options=weeks,
                    value=weeks[-1],
                    format_func=lambda value: value.strftime("%d/%m/%Y"),
                )
            if st.button("Atualizar leitura", width="stretch"):
                st.cache_data.clear()
                st.rerun()
            st.caption(
                f"**Período:** {settings.study.start_date:%d/%m/%Y} a "
                f"{settings.study.end_date:%d/%m/%Y}"
            )
        if section == "Visão geral":
            render_period(settings, source, identity, bounds)
        elif section == "Explorar por semana" and week is not None:
            with st.spinner("Lendo os indicadores da semana…"):
                table = geographic_subset(cached_week(settings, source, identity, week), bounds)
                summary = summarize(table)
            if not table.num_rows:
                st.info("Nenhuma célula disponível neste recorte geográfico.")
                return
            row = table.select(["week_end"]).slice(0, 1).to_pylist()[0]
            st.caption(
                f"SEMANA {max(week, settings.study.start_date):%d/%m/%Y} — "
                f"{min(row['week_end'], settings.study.end_date):%d/%m/%Y}"
            )
            render_metrics(summary)
            render_score_description()
            render_risk_scale()
            render_map(table, summary)
            with st.expander("Entender os fatores desta semana"):
                render_factors(summary)
        else:
            render_method(reader)
        st.divider()
        st.caption(
            "**Estudo exploratório:** não mede perdas de produção nem prescreve irrigação.<br>"
            "**Fontes:** NASA POWER, Sentinel-2, SoilGrids, MapBiomas e IBGE.",
            unsafe_allow_html=True,
        )
    except (ValueError, OSError, RuntimeError, DatabaseError, pa.ArrowInvalid) as error:
        LOGGER.error(
            "Dashboard read failed",
            extra={
                "source": source,
                "operation": "dashboard_read",
                "outcome": "failed",
                "error_type": type(error).__name__,
            },
        )
        if storage is None:
            st.error(
                "Configuração inválida da fonte do dashboard; confira "
                "WATER_STRESS_DASHBOARD__SOURCE e a localização S3."
            )
        elif storage.source == "s3":
            st.error(
                "Não foi possível ler a publicação do S3. Confira o bucket, o prefixo, "
                "a role da EC2, a publicação da Gold v1 e o espaço do cache. "
                "O dashboard não usa PostgreSQL neste modo."
            )
        else:
            st.error(
                "Não foi possível ler o PostgreSQL. Confira a conexão no .env, "
                "WATER_STRESS_DATABASE__ENABLED=true, as migrations 001-003 e a carga da Gold v1. "
                "O dashboard não migra nem carrega tabelas."
            )


if __name__ == "__main__":
    main()

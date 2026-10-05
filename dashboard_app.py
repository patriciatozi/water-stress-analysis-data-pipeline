"""Run with uv run --group dashboard streamlit run dashboard_app.py."""

from __future__ import annotations

import argparse
import logging
from datetime import date
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
)
from water_stress.dashboard.data import DashboardReader, DataSource
from water_stress.transformation import gold_weekly

LOGGER = logging.getLogger(__name__)
PAGE_TITLE = "Estresse hídrico e recomendação de irrigação na soja"
MAP_RISK_LABELS = {
    key: label for key, label in RISK_LABELS.items() if key not in ("pending", "missing")
}
MAP_CATEGORY_LABELS = {
    **MAP_RISK_LABELS,
    **{key: STATUS_LABELS[key] for key in ("complete", "partial")},
}

FACTOR_LABELS = {
    "precipitation_mm_7d": "Chuva",
    "eto_mm_7d": "ETo",
    "water_deficit_mm_7d": "Déficit hídrico",
    "ndvi_median": "NDVI",
    "ndmi_median": "NDMI",
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
h2, h3 {letter-spacing: -.025em;}
[data-testid="stMetric"] {background: white; border: 1px solid #e3e9ed;
    border-radius: 14px; padding: 18px 20px; min-height: 130px;}
[data-testid="stMetricValue"] {font-size: 2rem; letter-spacing: -.03em;}
[data-testid="stSidebar"] {border-right: 1px solid #e3e9ed;}
div[data-testid="stVerticalBlockBorderWrapper"] > div {border-radius: 14px;}
.st-key-map_categories :is([data-tag], [data-baseweb="tag"]) {
    background-color: #24745a !important; color: white !important;}
.st-key-map_categories :is([data-tag], [data-baseweb="tag"]):has([title="Completo"]),
.st-key-map_categories :is([data-tag], [data-baseweb="tag"]):has([title="Parcial"]) {
    background-color: #f4d46f !important; color: #594514 !important;}
.st-key-map_categories :is([data-tag], [data-baseweb="tag"]) button {color: inherit !important;}
.score-description {background: #e1efff; color: #0059b3; border-radius: 8px;
    padding: 16px; margin-bottom: 0; font-size: 1rem; line-height: 1.6;}
.score-description p {margin: 16px 0 0;}
.score-term {position: relative; display: inline-block;
    border-bottom: 1px dotted currentColor; cursor: help;}
.score-term:focus-visible {outline: 2px solid currentColor; outline-offset: 3px;}
.score-term [role="tooltip"] {visibility: hidden; opacity: 0; position: absolute;
    bottom: calc(100% + 8px); left: 0; z-index: 100;
    width: 260px; max-width: 75vw; padding: 10px 12px; border-radius: 8px;
    background: #233548; color: white; font-size: .875rem; line-height: 1.4;
    font-weight: normal; box-shadow: 0 4px 12px #23354826; pointer-events: none;}
.score-term:hover [role="tooltip"], .score-term:focus [role="tooltip"] {
    visibility: visible; opacity: 1;}
#eto-description {left: auto; right: 0;}
@media (max-width: 1400px) {
    .score-term [role="tooltip"], #eto-description {position: fixed;
        bottom: 24px; left: 50%; right: auto; transform: translateX(-50%);}
}
</style>
"""


def number(value: float | None, decimals: int = 1, suffix: str = "") -> str:
    if value is None or pd.isna(value):
        return "-"
    formatted = f"{value:,.{decimals}f}".replace(",", "_").replace(".", ",").replace("_", ".")
    return f"{formatted}{suffix}"


@st.cache_data(ttl=300, max_entries=2, show_spinner=False)
def cached_grid(_settings: Settings, identity: str) -> pa.Table:
    return DashboardReader(_settings).spatial_grid()


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
        font=dict(family="Arial, sans-serif", size=12, color="#536578"),
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
    colors: tuple[str, ...] = ("#24745a", "#558bb8", "#dba44a"),
) -> go.Figure:
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
        "Score médio · 0-100",
        number(None if summary.score is None else summary.score * 100),
        help=(
            "Média ponderada pela área de soja com score. Pode combinar completos e parciais; "
            "consulte a composição abaixo."
        ),
    )
    high.metric(
        "Área em alto ou crítico",
        number(
            summary.high_risk_area_km2
            if summary.scored_area_km2 > summary.pending_class_area_km2
            else None,
            0,
            " km²",
        ),
        help=(
            "Soma da área equivalente de soja nas classes Gold alto e crítico. "
            "Não é área com dano observado."
        ),
    )
    complete.metric(
        "Área com score completo",
        number(summary.complete_coverage_pct, suffix="%"),
        help=(
            "Área equivalente com todos os componentes / área equivalente total de soja no recorte."
        ),
    )


def render_score_description() -> None:
    st.html("""
        <div class="score-description">
            <strong>Como o score é composto?</strong>
            <p>Estimativa semanal do risco de estresse hídrico da soja, de 0 a 100:
            quanto maior o valor, maior o risco. Combina déficit entre
            <span class="score-term" tabindex="0" aria-describedby="eto-description">ETo<span
                role="tooltip" id="eto-description">Água que uma vegetação de referência
                perderia para o ar por evaporação e transpiração.</span></span>
            e chuva,
            <span class="score-term" tabindex="0" aria-describedby="ndvi-description">NDVI<span
                role="tooltip" id="ndvi-description">Indica o vigor e a cobertura da vegetação
                a partir de imagens de satélite.</span></span>
            e <span class="score-term" tabindex="0" aria-describedby="ndmi-description">NDMI<span
                role="tooltip" id="ndmi-description">Indica a umidade da vegetação a partir
                de imagens de satélite.</span></span>;
            resultados parciais usam os fatores disponíveis.</p>
        </div>
    """)


def render_map(table: pa.Table, summary: WeeklySummary) -> None:
    left, right = st.columns([2.15, 1], gap="large")
    with left, st.container(border=True):
        st.subheader("Mapa de riscos")
        chosen_categories = st.multiselect(
            "Categorias",
            list(MAP_CATEGORY_LABELS),
            default=list(MAP_RISK_LABELS),
            format_func=MAP_CATEGORY_LABELS.get,
            key="map_categories",
            help=(
                "Classes em verde; completude em amarelo. As classes escolhidas são combinadas "
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
            st.subheader("Área por classe")
            keys = list(MAP_RISK_LABELS)
            figure = go.Figure(
                go.Bar(
                    x=[risk_area[key] for key in keys],
                    y=[MAP_RISK_LABELS[key] for key in keys],
                    orientation="h",
                    marker_color=[RISK_COLORS[key] for key in keys],
                )
            )
            figure.update_yaxes(autorange="reversed")
            st.plotly_chart(
                style_chart(figure, height=260), width="stretch", config={"displayModeBar": False}
            )
            st.caption("Área equivalente de soja, em km².")
        with st.container(border=True):
            st.subheader("Composição dos resultados")
            st.caption(
                "Área de soja com os 3 fatores do score (completo) ou parte deles (parcial)."
            )
            areas = {
                "Completo": summary.complete_area_km2,
                "Parcial": summary.partial_area_km2,
            }
            for label, area in areas.items():
                st.write(f"**{label}** - {number(area, 0)} km²")


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
                marker_color=["#558bb8", "#8ca899", "#dba44a"],
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
    with vegetation, st.container(border=True):
        st.markdown("**Condição da vegetação e umidade espectral**")
        first, second = st.columns(2)
        first.metric("NDVI", number(summary.factors["ndvi_median"], 3))
        second.metric("NDMI", number(summary.factors["ndmi_median"], 3))
        st.caption(
            "NDVI e NDMI são médias ponderadas das medianas Gold disponíveis; faixa de -1 a 1."
        )
        st.caption(
            f"Cobertura NDVI: {number(summary.factor_coverage['ndvi_median'])}% · "
            f"NDMI: {number(summary.factor_coverage['ndmi_median'])}%"
        )
        st.write(f"Temperatura média: **{number(summary.factors['temperature_mean_c'])} °C**")


def render_evolution(
    settings: Settings, source: DataSource, identity: str, bounds: GeographicBounds | None
) -> None:
    st.subheader("Evolução semanal")
    with st.spinner("Resumindo as semanas disponíveis…"):
        data = period_data(cached_evolution(settings, source, identity, bounds), settings)
    left, right = st.columns(2, gap="large")
    with left, st.container(border=True):
        st.markdown("**Score por composição**")
        figure = line_chart(
            data,
            {
                "score": "Todos com score",
                "complete_score": "Completos",
                "partial_score": "Parciais",
            },
            scale=100,
            y_title="Score · 0-100",
        )
        figure.update_yaxes(range=[0, 100])
        st.plotly_chart(figure, width="stretch", config={"displayModeBar": False})
    with right, st.container(border=True):
        st.markdown("**Cobertura por área de soja**")
        figure = line_chart(
            data,
            {"coverage_pct": "Com score", "complete_coverage_pct": "Score completo"},
            y_title="% da área",
        )
        figure.update_yaxes(range=[0, 100])
        st.plotly_chart(figure, width="stretch", config={"displayModeBar": False})
    left, right = st.columns(2, gap="large")
    with left, st.container(border=True):
        st.markdown("**Chuva, ETo e déficit**")
        st.plotly_chart(
            line_chart(
                data,
                {
                    name: FACTOR_LABELS[name]
                    for name in ("precipitation_mm_7d", "eto_mm_7d", "water_deficit_mm_7d")
                },
                y_title="mm na janela",
                colors=("#558bb8", "#8ca899", "#dba44a"),
            ),
            width="stretch",
        )
    with right, st.container(border=True):
        st.markdown("**Índices espectrais**")
        st.plotly_chart(
            line_chart(data, {"ndvi_median": "NDVI", "ndmi_median": "NDMI"}, y_title="Índice"),
            width="stretch",
        )
    st.caption(
        "O recorte geográfico se mantém entre semanas. Cada variável usa sua área disponível; "
        "lacunas não são interpoladas. Semanas nas extremidades do estudo podem ter "
        "menos de sete dias."
    )


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
                colors=("#558bb8", "#8ca899", "#dba44a"),
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
        "O score v1 combina déficit entre ETo e chuva, NDVI e NDMI. É um indicador acadêmico "
        "provisório de condições associadas ao estresse hídrico da soja."
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
    st.write(
        "As classes vêm da Gold, antes do arredondamento: baixo até 25; atenção acima de 25 "
        "até 50; alto acima de 50 até 75; crítico acima de 75 até 100. "
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
    st.html(CSS)
    settings = load_settings(args.config)
    st.title(PAGE_TITLE)
    st.write("Região: Estado do Mato Grosso")
    with st.sidebar:
        st.subheader("Explorar os dados")
    source: DataSource = "postgres"
    reader = DashboardReader(settings, source)
    try:
        weeks = reader.weeks()
        identity = reader.fingerprint() + str(settings.study.model_dump()) + settings.config_hash
        with st.sidebar:
            week = st.select_slider(
                "Semana",
                options=weeks,
                value=weeks[len(weeks) // 2],
                format_func=lambda value: value.strftime("%d/%m/%Y"),
            )
            section = st.radio(
                "Visualização",
                ["Visão geral", "Sobre o indicador"],
                key="section",
            )
            bounds = None

            if st.button("Atualizar leitura", width="stretch"):
                st.cache_data.clear()
            st.caption(
                f"Período: {settings.study.start_date:%d/%m/%Y} a "
                f"{settings.study.end_date:%d/%m/%Y}"
            )
        with st.spinner("Lendo os indicadores da semana…"):
            table = geographic_subset(cached_week(settings, source, identity, week), bounds)
            summary = summarize(table)
        if not table.num_rows:
            st.info("Nenhuma célula disponível neste recorte geográfico.")
            return
        row = table.select(["week_end"]).slice(0, 1).to_pylist()[0]
        st.caption(
            f"SEMANA {max(week, settings.study.start_date):%d/%m/%Y} - "
            f"{min(row['week_end'], settings.study.end_date):%d/%m/%Y}"
        )
        render_metrics(summary)
        if section == "Visão geral":
            render_score_description()
            render_map(table, summary)
            render_factors(summary)
        elif section == "Evolução semanal":
            render_evolution(settings, source, identity, bounds)
        elif section == "Análise por célula":
            render_cell(settings, source, identity, week)
        else:
            render_method(reader)
        st.divider()
        st.caption("Fontes: NASA POWER, Sentinel-2, SoilGrids, MapBiomas e IBGE.")
    except (ValueError, FileNotFoundError, DatabaseError, pa.ArrowInvalid) as error:
        LOGGER.error(
            "Dashboard read failed",
            extra={
                "source": source,
                "operation": "dashboard_read",
                "outcome": "failed",
                "error_type": type(error).__name__,
            },
        )
        st.error(
            "Não foi possível ler o PostgreSQL. Confira a conexão no .env, "
            "WATER_STRESS_DATABASE__ENABLED=true, as migrations 001-003 e a carga da Gold v1. "
            "O dashboard não migra nem carrega tabelas."
        )


if __name__ == "__main__":
    main()

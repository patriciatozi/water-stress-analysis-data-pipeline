# Water Stress Analysis Data Pipeline

Pipeline acadêmico e reprodutível para integrar dados meteorológicos, de solo e de sensoriamento remoto usados na análise espaço-temporal do risco de estresse hídrico da soja.

> Este README é uma documentação viva. Ele deve ser atualizado sempre que o escopo, as fontes, as decisões técnicas ou os comandos do projeto mudarem.

## Visão do projeto

O objetivo de longo prazo é produzir uma base espaço-temporal capaz de apoiar estudos de déficit
hídrico e irrigação na agricultura de precisão. O projeto usa **Mato Grosso inteiro** como área de
estudo e evita materializar combinações diárias em alta resolução para todo o estado.

Recorte atual:

- área: estado de Mato Grosso;
- código IBGE da UF: `51`;
- período: `2023-09-01` a `2024-04-30`;
- cultura de interesse: soja;
- grade analítica: 1 km;
- grade de detalhamento futuro: 250 m;
- janela inicial do indicador: 7 dias;
- estágio atual: fundações Bronze/Silver para processamento estadual.

As saídas deste repositório são estimativas acadêmicas. Elas não constituem prescrição agronômica ou operacional de irrigação.

## Estado atual

| Componente | Estado | Implementação |
|---|---|---|
| Limite estadual IBGE | Implementado e testado | GeoJSON original de Mato Grosso |
| Meteorologia NASA POWER estadual | Implementado com mocks | Uma requisição regional por parâmetro |
| SoilGrids estadual | Implementado com mocks | WCS dividido em chunks de 250 km |
| Sentinel-2 L2A estadual | Implementado com mocks | Catálogo STAC; COGs desativados por padrão |
| MapBiomas | Implementado | Classificação anual nacional da Coleção 10 para derivar máscara de soja (classe 39) |
| Silver NASA POWER pontual | Legado validado | Transformação do piloto municipal anterior |
| Grade Silver estadual | Implementada e testada | GeoParquet de 1 km em SIRGAS 2000 / Brazil Polyconic |
| Silver `crop_mask` | Implementada e testada | Fração de soja MapBiomas por `grid_id` |
| Silver `soil_features` | Implementada e testada | Solo SoilGrids de 0–30 cm por `grid_id` |
| Silver `weather_daily` | Implementada e testada | Clima regional e ETo por célula e data |
| Silver `satellite_observation` | Implementada e testada localmente | NDVI/NDMI por cena e `grid_id` |
| Camada Gold semanal | Implementada inicialmente e testada | Features climáticas, solo, soja e Sentinel-2 opcionais |
| Persistência PostgreSQL/PostGIS | Implementada e testada com mocks | Manifestos Bronze e tabelas Silver/Gold relacionais |
| Dashboard Streamlit | Implementado e testado | Score v1, mapas, fatores e histórico; leitura local ou PostgreSQL |
| INMET | Fora do escopo atual | Fonte candidata para validação posterior |

Os smoke tests anteriores de Sorriso continuam como evidência dos clientes, mas seus artefatos
locais não correspondem à nova AOI estadual. A ingestão deve ser executada novamente nos novos
caminhos particionados.

## Dashboard de visualização

O painel Streamlit apresenta o score provisório v1, cobertura, classes de risco, clima,
NDVI/NDMI. Reutiliza os dados existentes, com leitura direta do PostgreSQL, sem reprocessar
o pipeline.

```bash
uv sync --group dashboard
uv run --group dashboard streamlit run dashboard_app.py
```

Abra `http://localhost:8501`. Consulte o [guia do dashboard](docs/dashboard.md) para conexão,
filtros, unidades, ponderação por área e limitações.

O dashboard também está incluído na imagem usada pelo Airflow, como serviço separado no
Docker Compose. Veja os [comandos para execução no Docker](docs/dashboard.md#executar-no-docker).

## Arquitetura estadual

Consulte o [guia de implementação](docs/implementation_guide.md) para entender os artefatos de
código, os fluxos de dados e as regras de negócio já implementadas.

```text
IBGE Mato Grosso --> dim_spatial_grid 1 km
        |
MapBiomas classe 39 --> crop_mask / soy_fraction (Silver futura)
        |
        +--> filtra tiles e células sem soja
                    |
       +------------+------------+
       |            |            |
  SoilGrids    NASA POWER    Sentinel-2
   chunks       regional       catálogo/tiles
   250 m         diário         10/20 m
       |            |            |
       +------------+------------+
                    |
           agregação semanal 1 km
                    |
                   Gold
```

As tabelas temáticas permanecem separadas (`dim_spatial_grid`, `crop_mask`, `soil_features`,
`weather_daily` e `satellite_observation`). Somente a Gold materializará as features
espaço-temporais necessárias ao score, sem repetir solo e geometria estáticos em cada data.

## Fontes ingeridas

### IBGE

O limite estadual oficial é a dependência espacial das demais fontes. A geometria original é
preservada em GeoJSON e usada para o bounding box regional da NASA POWER, os chunks do SoilGrids,
a grade de 1 km e a interseção com cenas Sentinel-2.

### NASA POWER

Variáveis meteorológicas diárias. A API regional recebe uma variável por requisição e limita cada
bounding box a 10° por eixo. Mato Grosso é dividido em quatro regiões; os sete parâmetros geram 28
artefatos independentes, que serão unidos e deduplicados na Silver por latitude, longitude e data:

- `T2M`, `T2M_MAX`, `T2M_MIN`;
- `RH2M`;
- `WS2M`;
- `ALLSKY_SFC_SW_DWN`;
- `PRECTOTCORR`.

### SoilGrids

Propriedades `clay`, `sand`, `soc` e `bdod`, nas profundidades `0-5cm`, `5-15cm` e `15-30cm`,
usando a estimativa mediana `Q0.5`. A extensão estadual projetada é dividida em chunks
configuráveis de 250 km antes das solicitações WCS.

### Sentinel-2 L2A

A busca usa o catálogo STAC Earth Search, cobertura de nuvens de até 30% e o período do estudo. No
modo estadual padrão, somente o catálogo é persistido. O download de COGs Bronze fica desativado
para evitar materializar imagens de todo o estado; o processamento transitório por tile, filtrado
pela máscara de soja, pertence à Silver.

O modo legado de cenas representativas pode ser habilitado com
`sentinel_2.download_bronze_assets: true`. Nesse modo, a seleção prioriza:

1. maior interseção com a AOI;
2. menor cobertura de nuvens;
3. data e identificador da cena como desempate.

Ativos baixados por cena: `red`, `nir`, `swir16` e `scl`. O download é feito em streaming, com checksum incremental, sem carregar o COG inteiro em memória.

### MapBiomas

A ingestão preserva o GeoTIFF nacional de cobertura e uso da terra da **Coleção 10**, ano de
referência **2023**, disponibilizado no Google Cloud Storage oficial. A classe de soja é o código
`39`, conforme a legenda oficial. O arquivo tem aproximadamente 762 MiB e é transferido em
streaming.

A Bronze mantém todas as classes originais. O recorte de Mato Grosso e a conversão para uma máscara
binária (`1 = soja`, `0 = demais classes`) serão feitos na Silver geoespacial, pois são
transformações derivadas. Os dados MapBiomas são disponibilizados sob licença CC BY 4.0 e devem
ser citados conforme as orientações oficiais do projeto.

## Arquitetura atual

```text
configs/
└── project.yml                 # recorte do estudo e parâmetros das fontes
src/water_stress/
├── config.py                   # configuração tipada com Pydantic
├── http.py                     # HTTP, retry, backoff e streaming
├── storage.py                  # abstração e implementação de armazenamento local
├── models.py                   # contratos dos resultados de ingestão
├── ingestion/
│   ├── common.py               # manifestos, checksums e idempotência
│   ├── ibge.py
│   ├── nasa_power.py
│   ├── soilgrids.py
│   ├── mapbiomas.py
│   └── sentinel_2.py
├── transformation/
│   ├── nasa_power.py          # transformação pontual legada
│   ├── spatial_grid.py        # grade estadual GeoParquet de 1 km
│   └── gold_weekly.py         # features semanais da Gold
└── pipelines/
    ├── run_ingestion.py
    └── run_transformation.py
tests/                          # testes unitários e de integração simulada
data/bronze/                    # dados locais ignorados pelo Git
data/silver/                    # dados padronizados locais ignorados pelo Git
```

Fluxo atual:

```text
Configuração YAML
      ↓
Limite estadual IBGE
      ├──→ bounding box → NASA POWER regional (1 parâmetro/requisição)
      ├──→ bounding box projetado → chunks SoilGrids WCS
      ├──→ geometria da busca → catálogo Sentinel-2 STAC
      └──→ grade analítica Silver de 1 km
      ↓
Arquivos originais + manifestos na camada Bronze
      ↓
Transformações temáticas Silver → Gold semanal particionada por semana
```

## Organização da camada Bronze

```text
data/bronze/
├── ibge/state/state_code=51/
│   ├── state.geojson
│   └── state.manifest.json
├── nasa_power/daily_regional/state_code=51/
│   └── start_date=2023-09-01/end_date=2024-04-30/
│       └── parameter={parameter}/
│           └── region_id={region_id}/
│               ├── weather.json
│               └── weather.manifest.json
├── soilgrids/state_code=51/
│   └── property={property}/depth={depth}/chunk_id={chunk_id}/
│       ├── {property}_{depth}_Q0.5.tif
│       └── {property}_{depth}_Q0.5.manifest.json
├── sentinel_2/l2a/state_code=51/
    ├── start_date=2023-09-01/end_date=2024-04-30/
    │   ├── search-results.json
    │   └── search-results.manifest.json
└── mapbiomas/land_cover/collection=10/year=2023/
    ├── brazil_coverage_2023.tif
    └── brazil_coverage_2023.manifest.json
```

Cada manifesto registra a origem, URL, parâmetros, instante UTC, status HTTP, tamanho, SHA-256, versão do projeto e hash da configuração. A execução padrão reutiliza somente arquivos cujo checksum e fingerprint da requisição continuam válidos. A opção `--force` cria uma versão imutável sem sobrescrever o artefato anterior.

## Camada Silver

### Grade espacial estadual

```text
data/silver/dim_spatial_grid/state_code=51/resolution_meters=1000/
├── grid.parquet
└── _metadata.json
```

O GeoParquet possui `grid_id` estável, geometria WKB em `EPSG:5880`, centróide em longitude e
latitude e área em km². O CRS projetado é obrigatório para que área e resolução sejam métricas.

```bash
uv run python -m water_stress.pipelines.run_transformation --source spatial-grid
```

### NASA POWER pontual legada

A transformação meteorológica produz uma linha para cada data do período configurado, inclui latitude e longitude, converte o valor de preenchimento `-999` em `null` e usa nomes semânticos em `snake_case`:

| Coluna | Unidade |
|---|---|
| `date` | data UTC |
| `latitude` | graus norte |
| `longitude` | graus leste |
| `temperature_mean_c` | °C |
| `temperature_max_c` | °C |
| `temperature_min_c` | °C |
| `relative_humidity_pct` | % |
| `wind_speed_ms` | m/s |
| `solar_radiation_mj_m2_day` | MJ/m²/dia |
| `precipitation_mm_day` | mm/dia |

As unidades e descrições são armazenadas nos metadados dos campos Parquet e também em `_schema.json`. O arquivo `_quality.json` registra quantidade de linhas, datas duplicadas e valores ausentes por coluna.

```text
data/silver/nasa_power/daily/{area_type}_code={area_code}/
├── _schema.json
├── _quality.json
├── year=2023/part-000.parquet
└── year=2024/part-000.parquet
```

### Máscara temática de soja

A transformação `crop-mask` cruza a classificação anual MapBiomas com a grade analítica e produz
uma linha por `grid_id` e ano. `soy_fraction` é uma fração adimensional entre 0 e 1, calculada pela
contagem dos centros dos pixels válidos da classe 39 que estão simultaneamente dentro do limite da
AOI e da célula da grade. Como os dados são categóricos, nenhuma interpolação é aplicada. A
aproximação por centro de pixel e a resolução da grade ficam registradas em `_metadata.json`.

```text
data/silver/crop_mask/state_code=51/year=2023/resolution_meters=1000/
├── part-000.parquet
├── _schema.json
├── _quality.json
└── _metadata.json
```

Pré-requisitos: limite IBGE, raster MapBiomas e `dim_spatial_grid` já materializados. Execute:

```bash
uv run python -m water_stress.pipelines.run_transformation --source spatial-grid
uv run python -m water_stress.pipelines.run_transformation --source crop-mask
```

### Atributos temáticos de solo

A transformação `soil-features` agrega os chunks SoilGrids de 250 m para a grade analítica de
1 km. Para cada profundidade, calcula a média dos pixels cujos centros estão dentro da geometria
estadual e da célula; depois combina 0–5, 5–15 e 15–30 cm pela espessura de cada intervalo. Não há
reamostragem ou interpolação dos rasters.
Chunks parciais nas bordas podem apresentar resolução efetiva ligeiramente diferente dos 250 m
nominais produzidos pelo WCS; o pipeline aceita variação máxima de 1%, registra o intervalo
observado e continua usando os centros dos pixels originais.
Como os GeoTIFFs WCS não declaram `nodata`, pixels mascarados e valores brutos menores ou iguais a
zero são tratados como ausentes; zero está fora do domínio físico válido das quatro propriedades
selecionadas. O relatório de qualidade registra nulos e intervalos finais por coluna.

| Coluna | Unidade Silver | Unidade SoilGrids | Conversão |
|---|---:|---:|---:|
| `clay_pct` | % | g/kg | × 0,1 |
| `sand_pct` | % | g/kg | × 0,1 |
| `soc` | g/kg | dg/kg | × 0,1 |
| `bulk_density` | g/cm³ | cg/cm³ | × 0,01 |

```text
data/silver/soil_features/state_code=51/resolution_meters=1000/
├── part-000.parquet
├── _schema.json
├── _quality.json
└── _metadata.json
```

Pré-requisitos: limite IBGE, chunks SoilGrids e `dim_spatial_grid` materializados. Execute:

```bash
uv run python -m water_stress.pipelines.run_transformation --source spatial-grid
uv run python -m water_stress.pipelines.run_transformation --source soil-features
```

### Meteorologia regional diária

A transformação `weather-daily` une os 28 artefatos regionais NASA POWER, recorta os centros das
células pela geometria de Mato Grosso e produz uma linha por `weather_cell_id` e data. Temperatura,
umidade, vento e precipitação usam a grade MERRA-2 de `0,5° × 0,625°`. A radiação SYN1DEG de
`1° × 1°` é atribuída pelo centro mais próximo, com distância máxima validada e registrada nos
metadados. Esse é o único método de harmonização espacial aplicado.

A evapotranspiração de referência diária (`reference_evapotranspiration_mm_day`) segue a equação
FAO-56 Penman–Monteith para superfície de referência gramada. A pressão atmosférica é estimada pela
elevação NASA POWER; a pressão real de vapor usa temperatura e umidade relativa médias. Se qualquer
entrada necessária estiver ausente, a ETo permanece nula. Para o passo diário, o fluxo de calor no
solo é zero e a razão entre radiação observada e céu claro é limitada ao intervalo FAO-56 de 0,3 a
1,0; essas premissas também são persistidas nos metadados.

```text
data/silver/weather_daily/state_code=51/start_date=2023-09-01/end_date=2024-04-30/
├── _schema.json
├── _quality.json
├── _metadata.json
├── year=2023/part-000.parquet
└── year=2024/part-000.parquet
```

```bash
uv run python -m water_stress.pipelines.run_transformation --source weather-daily
```

### Observações Sentinel-2

A transformação `satellite-observation` é incremental por cena. Primeiro cruza o footprint STAC
com as células de 1 km que possuem soja; em seguida usa a classe 39 do MapBiomas no nível do pixel,
aplica escala e offset L2A, máscara SCL e calcula NDVI e NDMI na grade B04/B08 de 10 m. B11 é
reamostrada de 20 m por interpolação bilinear; SCL e MapBiomas usam vizinho mais próximo por serem
categóricos. Os rasters intermediários não são persistidos.
Somente reflectâncias finitas e estritamente positivas após escala e offset são consideradas
válidas para os índices.

Classes SCL válidas: 4, 5, 6 e 7. Classes 8, 9 e 10 compõem o percentual de nuvens. Percentis 10,
50 e 90 são aproximados por histograma fixo de 400 classes entre -1 e 1, permitindo agregação em
streaming com memória limitada.

```text
data/silver/satellite_observation/state_code=51/
├── _schema.json
├── _metadata.json
└── year={year}/month={month}/tile_id={tile}/item_id={item_id}/
    ├── part-000.parquet
    └── _quality.json
```

O padrão seleciona uma cena de menor nebulosidade por mês e tile ao longo de todo o período de
estudo e limita a seleção a 15 tiles por mês. Isso evita que a Silver fique restrita ao primeiro
mês, mantendo o processamento incremental e o custo controlado:

```bash
uv run python -m water_stress.pipelines.run_transformation --source satellite-observation
```

Processar um item específico, um lote mensal controlado ou todo o catálogo:

```bash
uv run python -m water_stress.pipelines.run_transformation \
  --source satellite-observation --item-id S2B_21MVM_20230901_0_L2A
uv run python -m water_stress.pipelines.run_transformation \
  --source satellite-observation --max-items 5
uv run python -m water_stress.pipelines.run_transformation \
  --source satellite-observation --tiles-per-month 10 --max-items 30
uv run python -m water_stress.pipelines.run_transformation \
  --source satellite-observation --coverage all --max-items 20
```

A leitura usa COGs remotos quando os ativos não existem localmente. Reexecuções de itens concluídos
reutilizam o Parquet existente. `--tiles-per-month` limita a seleção mensal (10 por padrão),
priorizando tiles com maior quantidade estimada de células de soja cobertas e usando menor
nebulosidade como desempate. `--max-items` limita o lote total da execução depois da seleção
mensal; `--coverage monthly` é o padrão e `--coverage all` processa todos os itens do catálogo,
sem a seleção mensal. O limite mensal é cumulativo: tiles com Parquet e manifesto de qualidade já
existentes contam para a cota, e meses que já atingiram o limite são pulados.
O índice espacial de células de soja é calculado uma vez por execução e reutilizado entre cenas.
O processamento usa até 2 workers por padrão (`--workers`), enquanto a escrita dos Parquets e
manifestos permanece serializada.

### Gold semanal de features

A transformação `gold-weekly` cria uma linha por `grid_id` e semana iniciada na segunda-feira.
Somente células com `soy_fraction >= 0.25` entram na saída. A primeira versão calcula precipitação,
ETo, balanço hídrico, déficit, dias chuvosos, sequência seca, temperatura, solo e atributos de
soja. Observações Sentinel-2 são agregadas quando disponíveis. O Gold usa a observação mais recente
anterior ao fim da semana, sem olhar para o futuro, desde que a idade não ultrapasse
`gold.satellite_max_age_days` (30 dias por padrão); semanas sem observação válida permanecem nulas.

```text
data/gold/water_stress_weekly/state_code=51/
└── start_date=2023-09-01/end_date=2024-04-30/
    ├── week_start={YYYY-MM-DD}/part-000.parquet
    ├── _metadata.json
    └── _quality.json
```

Execute depois de materializar as tabelas Silver:

```bash
uv run python -m water_stress.pipelines.run_transformation --source gold-weekly
```

As fórmulas iniciais são `water_balance_mm_7d = precipitation_mm_7d - eto_mm_7d` e
`water_deficit_mm_7d = max(0, -water_balance_mm_7d)`. Elas são indicadores explicáveis, não uma
prescrição agronômica. O `water_stress_score` combina déficit, NDVI e NDMI normalizados, com pesos
configuráveis; os parâmetros atuais são provisórios e devem ser validados agronomicamente.

A Gold também produz `water_stress_risk_class` em quatro níveis: baixo (0–25), atenção
(>25–50), alto (>50–75) e crítico (>75–100), na escala visual. A política `four-level-v1`
classifica antes do arredondamento e gera orientações de monitoramento em português. A classe
legada `water_stress_class` e a fórmula atual permanecem disponíveis. Scores ausentes têm classe
nula e devem aparecer como “Sem dados suficientes”, separados de baixo risco.

O contrato `gold-consumption-v2` exige a migration `003_risk_classification.sql` para o consumo
relacional e a regeneração/recarga da Gold para preencher os novos campos. Os checkpoints detectam
a mudança de contrato. Consulte [o contrato e as regras de consumo](docs/implementation_guide.md#classificação-em-quatro-níveis)
para limites, versões, status e requisitos de apresentação. Esses campos não alteram a fórmula v1.

### Novo score com balanço diário e saída semanal

O método `academic-index-v2-surface-30cm` estima retenção de água, ETc por fase da soja,
chuva aproveitada e estresse diário em um reservatório de 30 cm. Publica resultados semanais para
três cenários de água inicial, combinando componente hídrico, NDVI e NDMI com pesos provisórios
50/30/20. Reutiliza clima Silver e features Gold locais, sem novo processamento Sentinel-2.
Calendário e condição inicial são hipóteses; não há validação de campo.

```bash
uv run python -m water_stress.pipelines.run_transformation --source gold-water-balance --max-cells 32
uv run python -m water_stress.pipelines.run_quality --dataset water_stress_weekly_v2 --max-cells 32
```

Tabelas novas: `gold.soil_hydraulics`, `gold.water_stress_weekly_v2`; consumo pela view
`gold.water_stress_dashboard_v2`, criada pela migration 004. Nenhum TRUNCATE é necessário:
a carga reconcilia apenas a análise carregada. O v1 continua disponível.
Consulte [fórmulas, contratos, piloto e comandos de carga](docs/score_v2.md).

## Preparação do ambiente

Requisitos:

- Python 3.12;
- [uv](https://docs.astral.sh/uv/).

Instale as dependências:

```bash
uv sync --all-groups
```

### Persistência PostgreSQL/PostGIS

O PostgreSQL/PostGIS é uma projeção relacional opcional dos dados. Os GeoTIFFs, COGs, GeoJSONs e
demais arquivos originais continuam em `data/bronze/` ou futuramente em object storage. O banco
recebe os manifestos Bronze, as tabelas Silver/Gold e o controle de execução; não recebe uma cópia
dos arquivos brutos.

O nome exibido para um servidor no pgAdmin é apenas um rótulo. A conexão real é determinada por
host, porta, database e usuário. O servidor precisa ter a extensão PostGIS instalada antes da
migration:

```sql
CREATE EXTENSION IF NOT EXISTS postgis;
```

Exemplo de configuração local em `.env` (não versionar):

```env
WATER_STRESS_DATABASE__ENABLED=true
WATER_STRESS_DATABASE__HOST=127.0.0.1
WATER_STRESS_DATABASE__PORT=5433
WATER_STRESS_DATABASE__NAME=water-stress
WATER_STRESS_DATABASE__USER=patriciatozi
WATER_STRESS_DATABASE__PASSWORD="sua_senha"
```

O valor da porta depende da instalação local. PostgreSQL Homebrew e EnterpriseDB podem usar portas
distintas. O pipeline usa `5432`/`water_stress` apenas como valores padrão do YAML.

O arquivo `.env` não é carregado automaticamente. Em cada nova sessão do terminal:

```bash
set -a
source .env
set +a
```

Aplique as migrations, registre os manifestos Bronze e carregue todas as tabelas derivadas:

```bash
uv run python -m water_stress.pipelines.run_database --migrate
uv run python -m water_stress.pipelines.run_database --register-bronze
uv run python -m water_stress.pipelines.run_database --load --dataset all
```

As tabelas ficam nos schemas `bronze`, `silver`, `gold` e `control`. A carga usa tabelas temporárias,
`COPY`, transação e `ON CONFLICT`, podendo ser repetida com segurança. Para testar uma tabela
individual:

```bash
uv run python -m water_stress.pipelines.run_database \
  --load --dataset weather_daily
```

O serviço PostgreSQL precisa estar executando antes desses comandos. Credenciais não devem ser
colocadas em `configs/project.yml` nem versionadas.

Consultas úteis no pgAdmin:

```sql
SELECT * FROM control.schema_migration;
SELECT dataset, status, row_count, error_message
FROM control.load_run
ORDER BY started_at_utc DESC;
SELECT COUNT(*) FROM silver.weather_daily;
SELECT COUNT(*) FROM gold.water_stress_weekly;
```

## Como executar

Planejar as saídas sem acessar as fontes ou gravar arquivos:

```bash
uv run python -m water_stress.pipelines.run_ingestion --dry-run
```

Executar todas as fontes:

```bash
uv run python -m water_stress.pipelines.run_ingestion
```

Executar uma fonte específica:

```bash
uv run python -m water_stress.pipelines.run_ingestion --source ibge
uv run python -m water_stress.pipelines.run_ingestion --source nasa-power
uv run python -m water_stress.pipelines.run_ingestion --source soilgrids
uv run python -m water_stress.pipelines.run_ingestion --source sentinel-2
uv run python -m water_stress.pipelines.run_ingestion --source mapbiomas
```

O download MapBiomas transfere aproximadamente 762 MiB para o ano configurado. A execução com
`--source all` também inclui esse arquivo.

As fontes NASA POWER, SoilGrids e Sentinel-2 dependem do limite IBGE existente. Ao executar `--source all`, essa ordem é resolvida automaticamente.

Forçar uma nova versão dos artefatos:

```bash
uv run python -m water_stress.pipelines.run_ingestion --force
```

Usar outra configuração:

```bash
uv run python -m water_stress.pipelines.run_ingestion --config caminho/project.yml
```

Transformar o Bronze NASA POWER em Silver:

```bash
uv run python -m water_stress.pipelines.run_transformation --source nasa-power
```

### Notebooks exploratórios

Os notebooks usam somente caminhos relativos e podem ser abertos na raiz do repositório:

```bash
uv run --group notebook jupyter lab notebooks/
```

| Notebook | Camada e objetivo |
|---|---|
| `01_explore_ibge_boundary.ipynb` | Bronze: geometria, extensão, ponto representativo e manifesto IBGE |
| `02_explore_nasa_power_bronze.ipynb` | Bronze: estrutura, unidades, qualidade e séries originais NASA POWER |
| `02_explore_nasa_power.ipynb` | Silver: schema, qualidade, precipitação, temperatura e sequências secas |
| `03_explore_soilgrids.ipynb` | Bronze: metadados, unidades, estatísticas e mapas dos 12 GeoTIFFs |
| `04_explore_sentinel_2.ipynb` | Bronze: catálogo STAC, COGs, SCL e diagnóstico exploratório de NDVI/NDMI |

O notebook Sentinel-2 pode ser aberto sem os arquivos locais: nesse caso, ele informa o comando
de ingestão e mantém as análises pendentes. Os índices espectrais calculados nele são apenas
diagnósticos; máscara de qualidade, recorte municipal e harmonização espacial serão formalizados
na camada Silver.

## Configuração

O arquivo [configs/project.yml](configs/project.yml) concentra área, período, propriedades, bandas, limites de nuvens, endpoints e política HTTP. Variáveis de ambiente com prefixo `WATER_STRESS_` podem substituir valores simples, por exemplo:

```bash
export WATER_STRESS_STORAGE__ROOT_PATH=/caminho/para/bronze
```

Não inclua credenciais ou caminhos pessoais no YAML versionado. Use variáveis de ambiente e mantenha `.env` fora do Git.

## Qualidade e testes

```bash
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run mypy src tests
```

Última validação local desta etapa:

- 92 testes aprovados;
- cobertura total de 85,98%;
- Ruff aprovado;
- mypy em modo estrito aprovado;
- smoke test real do SoilGrids aprovado;
- consulta e seleção reais do catálogo Sentinel-2 aprovadas.
- transformação real NASA POWER aprovada: 243 datas, duas partições anuais, sem duplicidades ou nulos.

Os testes automatizados não dependem da internet: as respostas HTTP e downloads são simulados. Smoke tests reais devem ser executados conscientemente, pois podem transferir arquivos grandes.

## Decisões e limitações conhecidas

- A Bronze preserva os dados de origem; recortes exatos, conversão de unidades e padronização pertencem à Silver.
- SoilGrids usa WCS porque a API REST beta está indisponível.
- O recorte SoilGrids atual usa o bounding box municipal; a máscara pela geometria exata será aplicada depois.
- O catálogo Sentinel-2 é persistido por padrão; COGs são processados incrementalmente por uma
  seleção mensal de menor nebulosidade ou, explicitamente, por todo o catálogo. Máscara SCL,
  escala/offset, reprojeção, NDVI e NDMI são calculados na Silver.
- O raster MapBiomas Bronze cobre todo o Brasil e preserva todas as classes. O recorte municipal e
  a máscara binária da classe 39 pertencem à futura Silver geoespacial.
- A NASA POWER estadual usa células regionais; a comparação com estações INMET permanece fora do
  escopo atual e poderá ser incorporada na validação futura.
- O armazenamento atual é local, mas está isolado por `StorageClient` para futura implementação em S3 ou ADLS.
- PostgreSQL/PostGIS é uma camada de consulta local opcional: Bronze bruto permanece em arquivos,
  enquanto manifestos, Silver e Gold são materializados relacionalmente.
- `migrations/001_initial.sql` é a fonte versionada do schema relacional; alterações de contrato
  devem gerar novas migrations e preservar compatibilidade quando possível.
- Cargas relacionais usam staging, transação e upsert por chave analítica; falhas não alteram os
  Parquets de origem.
- Arquivos em `data/` não devem ser enviados ao GitHub.

## Próximas etapas sugeridas

1. Validar agronomicamente os limites e pesos do score provisório.
2. Refinar a composição temporal do Sentinel-2 e a seleção entre cenas concorrentes.
3. Persistir a associação espacial entre `grid_id` e `weather_cell_id` se ela for reutilizada por
   outras tabelas.
4. Adicionar validação observacional, como INMET, sem alterar a Bronze existente.
5. Evoluir armazenamento e orquestração somente após estabilizar o MVP local.

## Como colaborar

- Abra uma issue ou descreva claramente a alteração proposta.
- Trabalhe em uma branch dedicada; evite commits diretos na `main` sem revisão.
- Mantenha mudanças pequenas e verificáveis.
- Adicione ou atualize testes junto com o código.
- Execute todas as verificações de qualidade antes de abrir um pull request.
- Atualize este README quando uma decisão, fonte, comando, limitação ou etapa mudar.
- Não versione dados brutos, segredos ou arquivos pessoais.
- Registre unidades, CRS, licenças e premissas metodológicas perto do código correspondente.

Sugestão de checklist para pull requests:

- [ ] testes, lint, formatação e tipagem passaram;
- [ ] novos comportamentos possuem testes;
- [ ] documentação e configuração foram atualizadas;
- [ ] unidades e CRS estão explícitos;
- [ ] nenhum dado bruto ou segredo foi adicionado;
- [ ] limitações e decisões relevantes foram registradas.

## Licenciamento e atribuição

O código-fonte está sob a [MIT License](LICENSE). Os conjuntos de dados não são redistribuídos pelo repositório e permanecem sujeitos às condições de seus provedores:

- [IBGE — API de Malhas](https://servicodados.ibge.gov.br/api/docs/malhas?versao=3);
- [NASA POWER](https://power.larc.nasa.gov/docs/services/api/temporal/daily/);
- [ISRIC SoilGrids WCS](https://docs.isric.org/globaldata/soilgrids/wcs.html);
- [Element 84 Earth Search](https://earth-search.aws.element84.com/v1).

## Automação com Airflow

O [guia de Airflow](docs/airflow.md) descreve a execução local via Docker Compose, com modos
`full`, `satellite-gold` e `gold-only`, validação Gold e carga opcional no PostgreSQL/PostGIS.
O DAG é manual para a safra histórica e mantém regras de negócio nos pipelines existentes.

# Guia do código e das regras de negócio

Este documento explica o que foi implementado, onde cada parte do código está e quais decisões de
negócio são aplicadas aos dados.

## 1. Objetivo e fluxo

O pipeline prepara dados para analisar risco de estresse hídrico da soja em Mato Grosso entre
`2023-09-01` e `2024-04-30`.

```text
IBGE + NASA POWER + SoilGrids + MapBiomas + Sentinel-2
                         │
                         ▼
             Bronze: arquivos originais
                         │
                         ▼
 Silver: grade + soja + solo + clima + índices de satélite
                         │
                         ▼
               Gold: features semanais iniciais
```

Hoje o código:

1. baixa e preserva dados originais;
2. cria uma grade estadual comum;
3. identifica onde existe soja;
4. agrega atributos de solo;
5. organiza meteorologia diária e calcula ETo;
6. calcula NDVI e NDMI nas áreas de soja.

## 2. Regras das camadas

### Bronze

- guarda o arquivo recebido da fonte sem transformação;
- nunca deve ser sobrescrita ou corrigida manualmente;
- reutiliza arquivos íntegros da mesma requisição;
- `--force` cria uma nova versão imutável;
- cada arquivo possui manifesto JSON;
- `data/` não é versionado no Git.

### Silver

- contém produtos derivados e prontos para análise;
- usa colunas em `snake_case`;
- documenta fonte, CRS, resolução, unidades e versão;
- valida duplicidades e contabiliza nulos;
- publica `quality_status` (`passed`, `warning` ou `failed`) e `quality_issues`;
- documenta agregação e reamostragem;
- grava por arquivo temporário antes de substituir o destino.

### Gold

`transformation/gold_weekly.py` produz a tabela `water_stress_weekly`, particionada por semana e
com chave `grid_id + week_start`. A versão inicial integra solo, fração de soja, meteorologia e
observações Sentinel-2 quando disponíveis. O score é provisório, configurável e deve ser validado
agronomicamente antes de uso analítico ou operacional.

### Persistência PostgreSQL/PostGIS

O PostgreSQL/PostGIS é uma projeção relacional opcional dos artefatos locais. A Bronze continua
imutável em arquivos ou object storage; somente seus manifestos são registrados em
`bronze.artifact_manifest`. As tabelas derivadas são carregadas nos schemas `silver` e `gold`, e as
execuções são rastreadas no schema `control`.

| Schema | Responsabilidade |
|---|---|
| `bronze` | manifesto, URL, checksum, período e versão da fonte |
| `silver` | grade, soja, solo, clima e observações Sentinel-2 |
| `gold` | features semanais para consulta analítica |
| `control` | migrations, execuções e contagens de carga |

As geometrias usam PostGIS: polígonos da grade em `EPSG:5880` e pontos meteorológicos em
`EPSG:4326`. O carregador usa tabelas temporárias, `COPY`, transações e `ON CONFLICT`; por isso a
execução é idempotente e pode ser reiniciada após uma falha.

O banco fica desabilitado por padrão. Para uma execução local, configure as variáveis
`WATER_STRESS_DATABASE__...` e carregue-as na sessão do terminal:

```bash
set -a
source .env
set +a
```

O arquivo `.env` não é versionado e não é carregado automaticamente pelo pipeline.

## 3. Configuração

Arquivo: `configs/project.yml`.

| Item | Valor atual | Uso |
|---|---|---|
| Área | Mato Grosso | Área de estudo |
| Código | `51` | Particionamento estadual |
| Período | 01/09/2023 a 30/04/2024 | Safra estudada |
| CRS de consulta | `EPSG:4326` | APIs e geometrias web |
| CRS métrico | `EPSG:5880` | Grade e áreas |
| Grade principal | 1.000 m | Chave espacial comum |
| Grade detalhada | 250 m | Uso futuro em hotspots |
| Janela analítica | 7 dias | Gold semanal com início na segunda-feira |
| Filtro de soja | `0.25` | Mínimo configurável de `soy_fraction` |
| Idade máxima Sentinel-2 | 30 dias | Limite para reutilizar a observação mais recente no Gold |
| Classe de soja | `39` | MapBiomas |
| Nuvem máxima | 30% | Busca Sentinel-2 |
| Tiles Sentinel-2 por mês | `10` | Limite padrão de cenas mensais na Silver |
| Workers Sentinel-2 | `2` | Concorrência local limitada para leitura das cenas |

O arquivo `src/water_stress/config.py` carrega o YAML com Pydantic e valida:

- código estadual com dois dígitos ou municipal com sete;
- data inicial menor ou igual à final;
- listas sem itens vazios ou repetidos;
- resoluções positivas;
- grade detalhada menor e divisora da grade principal.

## 4. Mapa do código

### Infraestrutura

| Arquivo | Responsabilidade |
|---|---|
| `config.py` | Configuração tipada |
| `http.py` | HTTP, timeout, retry e backoff |
| `storage.py` | Arquivos locais, checksum e escrita atômica |
| `logging.py` | Logs estruturados JSON |
| `models.py` | Resultados e estados das ingestões |
| `database/client.py` | Conexão e migrations PostgreSQL |
| `database/loader.py` | Registro Bronze e carga idempotente Silver/Gold |

### Bronze

| Arquivo | Fonte |
|---|---|
| `ingestion/ibge.py` | Limite IBGE |
| `ingestion/nasa_power.py` | NASA POWER pontual e regional |
| `ingestion/soilgrids.py` | SoilGrids WCS |
| `ingestion/sentinel_2.py` | Catálogo STAC e ativos Sentinel-2 |
| `ingestion/mapbiomas.py` | GeoTIFF MapBiomas |
| `ingestion/common.py` | Manifesto, fingerprint e idempotência |

### Silver

| Arquivo | Produto |
|---|---|
| `transformation/spatial_grid.py` | `dim_spatial_grid` |
| `transformation/crop_mask.py` | `crop_mask` |
| `transformation/soil_features.py` | `soil_features` |
| `transformation/weather_daily.py` | `weather_daily` estadual |
| `transformation/satellite_observation.py` | `satellite_observation` |
| `transformation/nasa_power.py` | NASA POWER pontual legado |
| `transformation/common.py` | Escrita atômica e metadados comuns da Silver |

Pontos de entrada:

- `pipelines/run_ingestion.py`: executa Bronze;
- `pipelines/run_transformation.py`: executa Silver e a Gold semanal.
- `pipelines/run_database.py`: aplica migrations, registra manifestos e carrega Parquet no PostGIS.

## 5. Componentes compartilhados

### HTTP

`src/water_stress/http.py` aplica:

- timeout de 120 segundos;
- até três tentativas;
- backoff de um segundo;
- identificação da fonte nos erros;
- streaming para downloads grandes;
- SHA-256 calculado durante o download.

### Armazenamento

`StorageClient` é a interface preparada para uma futura implementação S3 ou ADLS.
`LocalStorageClient` é a implementação atual.

Arquivos são escritos primeiro em um temporário. O destino só é substituído após o término da
escrita. Isso evita substituir um arquivo íntegro por um download interrompido.

O checksum local é calculado em blocos, sem carregar arquivos grandes inteiros na memória. O
tamanho e o checksum são consultados pela interface de armazenamento, sem acoplar a ingestão ao
filesystem local.

### Escrita e qualidade Silver

`transformation/common.py` centraliza somente operações compartilhadas pelas tabelas Silver:

- escrita atômica de JSON e Parquet com compressão Zstandard;
- documentação uniforme de schemas;
- contagem de nulos e faixas de valores;
- classificação comum de qualidade por chave analítica;
- validação dos arquivos exigidos por uma transformação.

As regras de qualidade são deliberadamente simples: duplicidades ou chaves nulas produzem
`failed`; valores ausentes em atributos produzem `warning`; ausência de problemas produz `passed`.
Valores ausentes ambientais não são convertidos automaticamente em zero.

As fórmulas e regras específicas continuam nos módulos temáticos. A orquestração Bronze aceita as
interfaces HTTP e de armazenamento por injeção, facilitando testes e uma futura implementação em
nuvem.

### Manifestos Bronze

Registram, conforme a fonte:

- URL, parâmetros e URL final;
- instante UTC e status HTTP;
- tamanho e SHA-256;
- área, período, CRS e resolução;
- versão e hash da configuração;
- fingerprint da requisição.

## 6. Ingestão por fonte

### IBGE

Código: `ingestion/ibge.py`.

- baixa o GeoJSON oficial de Mato Grosso;
- valida e combina geometrias quando necessário;
- preserva `EPSG:4326` na Bronze;
- fornece o limite exigido por NASA POWER, SoilGrids e Sentinel-2.

```text
data/bronze/ibge/state/state_code=51/state.geojson
```

### NASA POWER

Código: `ingestion/nasa_power.py`.

O bounding box estadual é dividido em quatro regiões. É feita uma requisição por região e
parâmetro:

```text
4 regiões × 7 parâmetros = 28 artefatos
```

| Código | Variável | Unidade |
|---|---|---|
| `T2M` | Temperatura média | °C |
| `T2M_MAX` | Temperatura máxima | °C |
| `T2M_MIN` | Temperatura mínima | °C |
| `RH2M` | Umidade relativa | % |
| `WS2M` | Vento a 2 m | m/s |
| `ALLSKY_SFC_SW_DWN` | Radiação solar | MJ/m²/dia |
| `PRECTOTCORR` | Precipitação corrigida | mm/dia |

Cada região e parâmetro é independente para permitir retry e reutilização.

### SoilGrids

Código: `ingestion/soilgrids.py`.

O limite é projetado para `ESRI:54052` e dividido em chunks de 250 km.

```text
4 propriedades × 3 profundidades × 30 chunks = 360 GeoTIFFs
```

Propriedades: argila (`clay`), areia (`sand`), carbono orgânico (`soc`) e densidade aparente
(`bdod`). Profundidades: 0–5, 5–15 e 15–30 cm. O quantil é a mediana `Q0.5`.

A resposta precisa ter assinatura TIFF ou BigTIFF válida.

### Sentinel-2

Código: `ingestion/sentinel_2.py`.

A busca STAC usa a coleção `sentinel-2-l2a`, período do estudo, geometria estadual, nuvens até 30%
e páginas de 100 itens.

O catálogo possui 3.128 itens. Por padrão, somente o catálogo é salvo. O download Bronze de COGs
está desabilitado para evitar materializar milhares de cenas.

Ativos necessários:

- B04 `red`: 10 m;
- B08 `nir`: 10 m;
- B11 `swir16`: 20 m;
- `scl`: 20 m.

### MapBiomas

Código: `ingestion/mapbiomas.py`.

Baixa o GeoTIFF nacional da Coleção 10, ano 2023. Todas as classes são preservadas na Bronze. A
classe 39, soja, é aplicada somente na Silver.

## 7. Tabelas Silver

### `dim_spatial_grid`

Código: `transformation/spatial_grid.py`.

Objetivo: criar a chave espacial comum.

| Coluna | Significado |
|---|---|
| `grid_id` | Identificador determinístico |
| `geometry` | Polígono WKB |
| `centroid_latitude` | Latitude central |
| `centroid_longitude` | Longitude central |
| `area_km2` | Área da célula |

Regras:

- limite chega em `EPSG:4326`;
- grade é calculada em `EPSG:5880`;
- cada célula mede 1.000 × 1.000 m;
- somente células que intersectam o estado são mantidas;
- células de borda continuam quadradas, sem recorte;
- `grid_id` deriva de estado, resolução, linha e coluna.

Resultado: 907.671 células.

### `crop_mask`

Código: `transformation/crop_mask.py`.

Objetivo: medir quanto de cada célula é soja.

```text
soy_fraction = pixels classe 39 / pixels MapBiomas válidos
```

Colunas: `grid_id`, `year` e `soy_fraction`.

Somente centros de pixels dentro de Mato Grosso e da célula são contados. Não há interpolação,
pois MapBiomas é categórico.

Resultado:

- 907.671 linhas;
- 212.500 células com soja;
- aproximadamente 104.780 km² equivalentes;
- 71 células periféricas sem centro válido ficam nulas.

### `soil_features`

Código: `transformation/soil_features.py`.

Objetivo: representar os primeiros 30 cm de solo em cada célula.

| Coluna | Unidade | Conversão |
|---|---|---|
| `clay_pct` | % | g/kg × 0,1 |
| `sand_pct` | % | g/kg × 0,1 |
| `soc` | g/kg | dg/kg × 0,1 |
| `bulk_density` | g/cm³ | cg/cm³ × 0,01 |

Cálculo:

1. média dos pixels de 250 m cujos centros caem na célula de 1 km;
2. média das profundidades ponderada pelas espessuras 5, 10 e 15 cm.

Regras:

- sem interpolação;
- exige as três profundidades para produzir uma propriedade;
- valores brutos `<= 0` são ausentes porque os TIFFs observados não declaram `nodata`;
- chunks de borda podem variar até 1% dos 250 m nominais;
- variação maior interrompe o processamento;
- rasters de um mesmo chunk precisam estar alinhados.

Resultado: 907.671 linhas, 905.639 completas.

### `weather_daily`

Código: `transformation/weather_daily.py`.

Objetivo: produzir meteorologia diária estadual.

Chave: `weather_cell_id + date`.

MERRA-2 usa grade `0,5° × 0,625°`; a radiação SYN1DEG usa `1° × 1°`.

Harmonização:

- MERRA-2 é a referência;
- cada célula recebe a radiação do centro SYN1DEG mais próximo;
- distância máxima observada: `0,7071°`.

Valores `-999` viram nulos.

#### ETo

`reference_evapotranspiration_mm_day` usa FAO-56 Penman–Monteith.

Premissas:

- superfície gramada de referência;
- pressão estimada pela elevação NASA POWER;
- vapor real estimado por temperatura e umidade médias;
- vento medido a 2 m;
- fluxo diário de calor no solo igual a zero;
- `Rs/Rso` limitado entre 0,3 e 1,0;
- entrada ausente produz ETo nula.

Resultado: 241 células × 243 datas = 58.563 linhas, particionadas em 2023 e 2024.

### `satellite_observation`

Código: `transformation/satellite_observation.py`.

Objetivo: calcular índices somente nos pixels de soja e agregar por `grid_id`.

Chave: `grid_id + date + tile_id + item_id`.

Colunas:

- média, P10, P50 e P90 de NDVI;
- média, P10, P50 e P90 de NDMI;
- pixels de soja e pixels válidos;
- percentual válido e percentual de nuvens;
- data, tile e item STAC.

Fluxo:

1. seleciona células com `soy_fraction > 0`;
2. cruza centros das células com o footprint da cena;
3. lê B04, B08, B11 e SCL localmente ou dos COGs públicos;
4. projeta MapBiomas classe 39 para a grade Sentinel;
5. processa blocos de 512 × 512 pixels;
6. aplica soja, SCL, escala e offset;
7. calcula NDVI e NDMI;
8. agrega diretamente para `grid_id`;
9. grava somente a tabela.

Resolução e máscara:

- B04 e B08 ficam em 10 m;
- B11 passa de 20 m para 10 m com bilinear;
- SCL e MapBiomas usam vizinho mais próximo;
- escala `0,0001` e offset `-0,1` são aplicados antes dos índices;
- reflectâncias precisam ser finitas e positivas;
- SCL 4, 5, 6 e 7 são válidas;
- SCL 8, 9 e 10 são nuvens;
- demais classes são inválidas.

```text
NDVI = (NIR - RED) / (NIR + RED)
NDMI = (NIR - SWIR16) / (NIR + SWIR16)
```

P10, P50 e P90 são aproximados por histograma de 400 classes entre -1 e 1. A precisão é cerca de
0,005 e reduz o uso de memória.

O padrão seleciona a cena de menor nebulosidade para cada combinação de ano, mês e tile, cobrindo
todo o período configurado, e mantém no máximo 15 tiles por mês. A seleção prioriza a maior
quantidade estimada de células de soja cobertas pelo footprint; `--tiles-per-month` permite ajustar
esse limite, `--max-items` limita o lote total da execução e `--item-id` escolhe cenas explicitamente.
`--coverage all` desativa a seleção mensal e processa todos os itens do catálogo. O limite é
cumulativo por mês: partições completas existentes contam para a cota, e meses completos são
ignorados em reexecuções.
Partições existentes e legíveis são reutilizadas. Nenhum raster intermediário é persistido.
O índice das células candidatas é montado uma vez por `tile_id` e reutilizado nas cenas do mesmo
tile. O processamento usa no máximo dois workers por padrão; `--workers` ajusta esse limite, e a
persistência dos artefatos permanece serializada para preservar escritas atômicas.

Podem existir vários itens na mesma data e tile. A regra de mosaico ou prioridade deve ser definida
antes da Gold.

## 8. Estrutura dos dados

```text
data/bronze/
├── ibge/state/state_code=51/
├── nasa_power/daily_regional/state_code=51/
├── soilgrids/state_code=51/property={property}/depth={depth}/chunk_id={chunk}/
├── sentinel_2/l2a/state_code=51/
└── mapbiomas/land_cover/collection=10/year=2023/

data/silver/
├── dim_spatial_grid/state_code=51/resolution_meters=1000/
├── crop_mask/state_code=51/year=2023/resolution_meters=1000/
├── soil_features/state_code=51/resolution_meters=1000/
├── weather_daily/state_code=51/start_date=2023-09-01/end_date=2024-04-30/
└── satellite_observation/state_code=51/year={year}/month={month}/
    └── tile_id={tile}/item_id={item_id}/
```

Arquivos auxiliares Silver:

- `_schema.json`: colunas, tipos, descrições e unidades;
- `_quality.json`: linhas, nulos, duplicidades e intervalos;
- `_metadata.json`: fonte, CRS, resolução, método e versão.

O PostgreSQL não substitui essa estrutura de arquivos: ele materializa uma cópia consultável das
tabelas derivadas e dos manifestos, mantendo os artefatos de origem fora do banco.

## 9. Ordem de execução

```bash
uv sync --all-groups

uv run python -m water_stress.pipelines.run_ingestion --source ibge
uv run python -m water_stress.pipelines.run_ingestion --source nasa-power
uv run python -m water_stress.pipelines.run_ingestion --source soilgrids
uv run python -m water_stress.pipelines.run_ingestion --source sentinel-2
uv run python -m water_stress.pipelines.run_ingestion --source mapbiomas

uv run python -m water_stress.pipelines.run_transformation --source spatial-grid
uv run python -m water_stress.pipelines.run_transformation --source crop-mask
uv run python -m water_stress.pipelines.run_transformation --source soil-features
uv run python -m water_stress.pipelines.run_transformation --source weather-daily
uv run python -m water_stress.pipelines.run_transformation --source satellite-observation
uv run python -m water_stress.pipelines.run_transformation --source gold-weekly

# Persistência relacional opcional
uv run python -m water_stress.pipelines.run_database --migrate
uv run python -m water_stress.pipelines.run_database --register-bronze
uv run python -m water_stress.pipelines.run_database --load --dataset all
```

O carregador relacional lê os arquivos Parquet em lotes de até 10 mil linhas. Cada dataset é
copiado para uma tabela temporária e aplicado por `UPSERT` dentro de uma transação própria;
repetir a carga após uma interrupção é seguro e não duplica as chaves analíticas.

Dependências:

- NASA POWER, SoilGrids e Sentinel-2 precisam do limite IBGE;
- `crop_mask` precisa da grade e MapBiomas;
- `soil_features` precisa da grade e SoilGrids;
- `weather_daily` precisa do limite e dos 28 artefatos NASA POWER;
- `satellite_observation` precisa da grade, `crop_mask`, MapBiomas e catálogo Sentinel-2.

## 10. Opções

Ingestão:

```text
--source     fonte ou all
--dry-run    planeja sem baixar ou gravar
--force      cria nova versão imutável
--config     seleciona outro YAML
```

Transformação:

```text
--source       produto Silver
--config       seleciona outro YAML
--item-id      item Sentinel-2; pode ser repetido
--max-items    limite opcional do lote total Sentinel-2
--tiles-per-month  máximo de tiles selecionados por mês (15 por padrão)
--workers      máximo de processadores concorrentes (2 por padrão)
--coverage     monthly (padrão) ou all
```

`--force` não existe nas transformações Silver.

Persistência:

```text
--migrate          aplica migrations SQL pendentes
--register-bronze  registra manifestos sem copiar arquivos brutos
--load             carrega datasets derivados
--dataset          dataset individual ou all
--migration-dir    diretório de migrations; padrão migrations/
```

## 11. Testes

```bash
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run mypy
```

Os testes cobrem configuração, HTTP, retry, erros, checksum, idempotência, geometrias, grade,
conversões de solo, soja, meteorologia, ETo, SCL, escala, offset, NDVI, NDMI, partições e metadados.

Última validação registrada: 92 testes, cobertura de 85,98%, Ruff, formatação e mypy aprovados.

## 12. Notebooks

| Notebook | Uso |
|---|---|
| `01_explore_ibge_boundary.ipynb` | Limite IBGE |
| `02_explore_nasa_power_bronze.ipynb` | NASA POWER original |
| `02_explore_nasa_power.ipynb` | Silver pontual legada |
| `03_explore_soilgrids.ipynb` | Rasters de solo |
| `04_explore_sentinel_2.ipynb` | Catálogo, bandas e índices |

```bash
uv run --group notebook jupyter lab notebooks/
```

## 13. Ainda não implementado

- calibração agronômica dos parâmetros do score;
- composição temporal definitiva e prioridade entre itens Sentinel da mesma data e tile;
- associação persistida de `grid_id` com `weather_cell_id`;
- grade adaptativa de 250 m;
- validação com INMET;
- armazenamento S3 ou ADLS.

## 14. Regras que não devem mudar silenciosamente

- Bronze nunca deve ser transformada ou sobrescrita.
- Áreas devem ser calculadas em CRS métrico, não em graus.
- Rasters categóricos usam vizinho mais próximo.
- Bilinear é usada somente para a reflectância contínua B11.
- Toda reamostragem deve ser documentada.
- SoilGrids `<= 0` é ausente enquanto a fonte não declarar `nodata`.
- ETo depende das premissas FAO-56 registradas.
- Sentinel-2 deve continuar incremental até existir estimativa de custo e tempo.
- Dados locais nunca devem ser adicionados ao Git.

## Gold de consumo — índice acadêmico v1, contrato v2

`water_stress_weekly` mantém uma linha por `grid_id + week_start`, para células com
`soy_fraction >= 0,25`. Semanas começam segunda-feira; extremos usam somente datas do estudo.
O índice final do pipeline permanece sujeito a calibração agronômica. Solo é contexto, não
componente do índice; não há modelagem de armazenamento no solo, estágio fenológico ou ETc.

- Déficit: `max(0, ETo - P)` em mm no período, componente `clip(déficit / 20, 0, 1)`.
- NDVI: `clip((0,7 - NDVI) / 0,7, 0, 1)`; NDMI: `clip((0,2 - NDMI) / 0,2, 0, 1)`.
- Score: média ponderada dos componentes disponíveis, pesos padrão 0,5 / 0,3 / 0,2.
  Componentes com peso zero são excluídos da contagem. Valores zero são válidos.
- Classe legada (`water_stress_class`): `low` abaixo de 1/3, `moderate` de 1/3 a menos de 2/3,
  `high` a partir de 2/3. Preservada para consumidores do contrato v1.
- Meteorologia: centro NASA POWER mais próximo em coordenadas geográficas, sem interpolação.
  Essa associação não representa distância métrica. Precipitação e ETo devem existir para
  todos os dias esperados; caso contrário, score e classe são nulos, status `unavailable`.
- Sentinel-2: última data até o fim da semana, idade máxima 30 dias; cenas concorrentes nessa
  data usam mediana dos índices médios e média dos percentuais, sem mosaico de pixels.
  Ausência de satélite permite score `partial`; todos os componentes configurados dão `complete`.
  `score_available_weight` informa a fração dos pesos disponíveis, não confiança estatística.

### Classificação em quatro níveis

`water_stress_risk_class` interpreta o mesmo índice pela política `four-level-v1`, implementada
em `water_stress.risk_classification`, sem alterar fórmula ou pesos. O score é adimensional;
a escala 0–100 da view é uma transformação de apresentação, não probabilidade de estresse.
Os limites são provisórios e ainda exigem validação agronômica.

| Score 0–100 | Código | Rótulo | `monitoring_guidance` |
|---|---|---|---|
| 0 ≤ score ≤ 25 | `low` | Baixo | Manter monitoramento de rotina. |
| 25 < score ≤ 50 | `attention` | Atenção | Monitorar a tendência do indicador. |
| 50 < score ≤ 75 | `high` | Alto | Priorizar avaliação das condições da área. |
| 75 < score ≤ 100 | `critical` | Crítico | Avaliar as condições da área com urgência. |

A classificação usa o valor original entre 0 e 1, antes de qualquer arredondamento: 25,7 na
escala visual é `attention`. Zero é válido e corresponde a `low`. Scores não finitos ou fora
de [0, 1] são rejeitados. Score ausente mantém as duas classes nulas e recebe a orientação
“Sem dados suficientes para classificar o risco.” A versão da política é registrada também
nessas linhas, pois a ausência faz parte do contrato; não existe classe de risco `unavailable`.

Campos novos: `water_stress_risk_class` (string nullable), `risk_classification_version`
(string preenchida nas novas saídas) e `monitoring_guidance` (texto em português preenchido nas
novas saídas). A chave `grid_id + week_start`, grade, unidades e partições permanecem iguais.
Schema e metadados registram `gold-consumption-v2`, método `academic-index-v1`, versão da
classificação, limites, rótulos, orientações e política de intervalos/ausência. A assinatura dos
checkpoints inclui o contrato e a política completa: outputs v1 e mudanças na classificação
exigem recálculo; outputs v2 íntegros com os mesmos inputs e política são reutilizados.

O índice ainda usa ETo e chuva total, limites fixos de NDVI/NDMI e três componentes. ETc,
chuva efetiva, anomalias históricas/fenológicas, retenção hídrica do solo e persistência do estresse
continuam pendentes de métodos, dados e validação. A sequência de dias sem chuva não mede a
persistência do estresse. A nova classificação não acrescenta esses sinais ao cálculo.

O [plano de implementação do balanço diário](score_v2_implementation_plan.md) registra a evolução
proposta usando somente os dados locais existentes, com saída semanal. As escolhas confirmadas
são Kc por fase com calendário de cenário, score combinado e três condições iniciais de água.
Esse modelo ainda não foi implementado e não altera o índice vigente.

Saída: Parquet Zstandard por semana, schema, qualidade e metadados com checksums dos inputs.
Checkpoints por semana reutilizam somente outputs íntegros com os mesmos inputs e parâmetros.
Uma mudança de dados ou parâmetros recalcula as partições. A grade usa EPSG:5880 e resolução
configurada (padrão 1 km); a tabela Gold referencia a geometria por chave.

Para gerar os arquivos e atualizar o serving opcional:

```bash
uv run python -m water_stress.pipelines.run_transformation --source gold-weekly
uv run python -m water_stress.pipelines.run_database --migrate
uv run python -m water_stress.pipelines.run_database --load --dataset water_stress_weekly
```

A migration `002_gold_consumption.sql` adiciona o contrato e cria
`gold.water_stress_dashboard`, com centróides, geometria EPSG:5880, área equivalente de soja
(`area_km2 * soy_fraction`) e score percentual. Após migrar, regenere e recarregue a Gold;
linhas antigas ficam com status nulo até a recarga. O dashboard deve filtrar por semana,
exibir indisponíveis separadamente e permitir filtrar `score_status`; scores parciais podem
ter pesos diferentes entre células. Para agregar risco estadual, use média ponderada pela
área equivalente de soja e publique a cobertura de área com score junto à média.

A migration `003_risk_classification.sql` acrescenta os três campos novos à tabela e ao final
da view, preservando nomes, ordem e tipos dos campos anteriores e as views dependentes. Não
reclassifica linhas existentes: os novos campos ficam nulos até regenerar e carregar a Gold.
O carregador aceita arquivos v1 com os três campos ausentes e grava nulos nesses campos; para
obter a nova classificação, use arquivos regenerados v2. Recarregar arquivos v1 sobre linhas v2
também torna esses campos nulos. A migration deve ser aplicada antes de usar o carregador atualizado.
Os comandos acima aplicam as migrations pendentes e regeneram os arquivos por checkpoint, sem
alterar a Bronze nem repetir a transformação Silver.

### Consumo pelo dashboard

O consumidor Streamlit está implementado em `dashboard_app.py`, com leitura/validação em
`water_stress.dashboard.data` e agregações independentes da interface em
`water_stress.dashboard.analysis`. Usa o score v1; o balanço v2 permanece separado.
O painel consulta diretamente o PostgreSQL, sem seleção de fonte nem fallback para arquivos.
Também pode executar no serviço `dashboard` do Compose, compartilhando a imagem do Airflow,
sem montar datasets locais nem iniciar os serviços de orquestração.
Execução, conexão e regras de apresentação: [guia do dashboard](dashboard.md).

- Usar `water_stress_risk_class` para a legenda de quatro níveis e `monitoring_guidance` para
  a orientação; cores, rótulos traduzidos, filtros e arredondamento pertencem à apresentação.
- Exibir score nulo como “Sem dados suficientes”, separado de risco baixo. Se a versão da
  classificação for nula, informar “Classificação pendente de atualização”, sem inferir a classe
  no cliente a partir de dados v1. Classificar não é arredondar o número exibido.
- Mostrar `score_status`, `score_component_count`, `score_available_weight` e `satellite_age_days`.
  Permitir separar completos e parciais; `complete` indica componentes disponíveis, não validação
  agronômica. As orientações são acadêmicas, sem recomendação automática de irrigação.
- Nas médias estaduais, ponderar pela área equivalente de soja com score disponível e publicar
  a cobertura de área com score. Distribuições por classe usam a classe de cada célula e sua área;
  a classe de uma média estadual não substitui a distribuição espacial.

```sql
SELECT grid_id, centroid_latitude, centroid_longitude,
       water_stress_score_pct, water_stress_risk_class, risk_classification_version,
       monitoring_guidance, score_status, score_component_count,
       score_available_weight, satellite_age_days
FROM gold.water_stress_dashboard
WHERE week_start = DATE '2023-09-04';
```

## Orquestração Airflow

`dags/water_stress_pipeline.py` coordena os CLIs existentes em quatro modos: execução completa,
Sentinel-2 + Gold, somente Gold e somente balanço hídrico. O gate `pipelines/run_quality.py` impede publicação de Gold
vazia, relatório inválido ou qualidade `failed`, permitindo `warning` documentado. A carga no banco
é opcional e segue migrations → manifestos → datasets em ordem de dependência.
Instalação, comandos e limites operacionais: [guia Airflow](airflow.md).

## Score com balanço diário superficial

O método `academic-index-v2-surface-30cm` foi implementado em datasets separados, preservando v1.
O [contrato do score v2](score_v2.md) especifica parâmetros, fórmulas, unidades, políticas de nulos,
schemas, chaves, linhagem, retomada, piloto e comandos. A migration 004 cria tabelas e view aditivas;
a carga reconcilia por `analysis_id`, sem TRUNCATE. Selecionar uma análise e cenário no consumo.

# Memória de desenvolvimento — Water Stress Analysis Data Pipeline

> Documento portátil para retomar o projeto em outra sessão ou compartilhar o contexto com o
> ChatGPT Web. Atualize-o quando houver mudanças relevantes de escopo, arquitetura ou estado.

## Estado atual

O projeto implementa um pipeline acadêmico e reprodutível para integrar meteorologia, solo, uso da
terra e sensoriamento remoto na análise espaço-temporal do risco de estresse hídrico da soja.

Escopo vigente:

- área: estado de Mato Grosso (`state_code=51`);
- período: `2023-09-01` a `2024-04-30`;
- cultura: soja;
- grade analítica principal: 1 km;
- CRS métrico: SIRGAS 2000 / Brazil Polyconic (`EPSG:5880`);
- consultas a APIs: `EPSG:4326`;
- execução local preparada para futura migração a S3 ou ADLS.

O piloto municipal de Sorriso (`5107925`) foi a primeira versão e permanece apenas como contexto
histórico. A configuração e a arquitetura atuais são estaduais. Os resultados são acadêmicos e não
constituem prescrição agronômica, previsão operacional ou recomendação de irrigação.

## Arquitetura

- Bronze preserva respostas originais e imutáveis.
- Silver contém dados padronizados, validados e agregados nas granularidades apropriadas.
- Gold integra os atributos necessários em janelas semanais e mantém score provisório explícito.
- Dados baixados e produtos em `data/` não são versionados.
- Cada dataset registra fonte, extração, CRS, resolução, unidades e versão de processamento.
- PostgreSQL/PostGIS materializa uma projeção relacional opcional: manifestos Bronze, tabelas Silver,
  Gold e controle de execução. Os arquivos brutos continuam sendo a fonte preservada.

Tabelas atuais:

- `dim_spatial_grid`: dimensão espacial comum;
- `crop_mask`: fração anual de soja;
- `soil_features`: atributos estáticos de solo;
- `weather_daily`: meteorologia diária por célula NASA POWER;
- `satellite_observation`: índices espectrais por célula, cena e tile.
- `gold.water_stress_weekly`: features semanais e score provisório.

Persistência relacional:

- `bronze.artifact_manifest`: linhagem dos arquivos Bronze, sem copiar os bytes brutos;
- `silver.*`: tabelas derivadas com chaves estrangeiras para `dim_spatial_grid`;
- `gold.water_stress_weekly`: tabela analítica com chave `grid_id + week_start`;
- `control.schema_migration`, `control.load_run` e `control.dataset_load`: auditoria técnica das
  migrations e cargas;
- PostGIS usa `EPSG:5880` para polígonos da grade e `EPSG:4326` para pontos meteorológicos;
- staging temporário, `COPY`, transação e `ON CONFLICT` tornam a carga idempotente e reiniciável.

Consulte `docs/data_architecture.md` para o modelo lógico detalhado.

## Camada Bronze

### IBGE

- GeoJSON oficial do limite de Mato Grosso.
- A geometria dirige consultas, chunks, recortes, grade e seleção de cenas.

### NASA POWER

- API regional diária.
- Variáveis: `T2M`, `T2M_MAX`, `T2M_MIN`, `RH2M`, `WS2M`,
  `ALLSKY_SFC_SW_DWN` e `PRECTOTCORR`.
- Bounding box estadual dividido em quatro regiões.
- Uma requisição por região e parâmetro: 28 artefatos.
- Grades nativas: MERRA-2 (`0,5° × 0,625°`) e SYN1DEG (`1° × 1°`) para radiação.

### SoilGrids

- Propriedades: `clay`, `sand`, `soc` e `bdod`.
- Profundidades: `0-5cm`, `5-15cm` e `15-30cm`.
- Quantil `Q0.5`, via WCS em chunks de 250 km.
- 360 GeoTIFFs: 4 propriedades × 3 profundidades × 30 chunks.
- CRS configurado: `ESRI:54052`; resolução nominal: 250 m.

### Sentinel-2 L2A

- Catálogo Earth Search STAC com limite de nuvens de 30%.
- Catálogo estadual validado: 3.128 itens, sem IDs duplicados.
- B04 e B08: 10 m; B11 e SCL: 20 m.
- O padrão estadual persiste somente o catálogo, sem baixar COGs em massa.
- A Silver acessa COGs públicos remotamente ou reutiliza ativos locais.

### MapBiomas

- Coleção 10, ano 2023, GeoTIFF nacional original.
- Classe de soja: `39`.
- Licença: CC BY 4.0.

A ingestão é idempotente. Artefatos íntegros são reutilizados e `--force` cria uma versão imutável
sem sobrescrever o arquivo anterior.

## Camada Silver implementada

### `dim_spatial_grid`

- 907.671 células de 1 km que intersectam Mato Grosso.
- GeoParquet em `EPSG:5880`.
- `grid_id` determinístico, geometria WKB, centróide e área em km².

```bash
uv run python -m water_stress.pipelines.run_transformation --source spatial-grid
```

### `crop_mask`

- Uma linha por `grid_id` e ano.
- `soy_fraction` entre 0 e 1.
- Classe 39 agregada pela contagem de centros de pixels válidos dentro da AOI.
- Nenhuma interpolação categórica.
- Smoke test: 907.671 linhas e 212.500 células com soja.
- Área equivalente aproximada: 104.780 km².

```bash
uv run python -m water_stress.pipelines.run_transformation --source crop-mask
```

### `soil_features`

| Coluna | Unidade | Origem e conversão |
|---|---|---|
| `clay_pct` | % | `clay` em g/kg × 0,1 |
| `sand_pct` | % | `sand` em g/kg × 0,1 |
| `soc` | g/kg | `soc` em dg/kg × 0,1 |
| `bulk_density` | g/cm³ | `bdod` em cg/cm³ × 0,01 |

- Média espacial dos centros de pixels por célula de 1 km.
- Média vertical ponderada pelas espessuras 5, 10 e 15 cm para representar 0–30 cm.
- Sem reamostragem ou interpolação.
- Variações de até 1% na resolução de chunks parciais são validadas e registradas.
- Valores brutos `<= 0` são ausentes, pois os TIFFs WCS observados não declaram `nodata`.
- Smoke test: 907.671 linhas, 905.639 completas e nenhuma duplicidade.

```bash
uv run python -m water_stress.pipelines.run_transformation --source soil-features
```

### `weather_daily`

- Uma linha por `weather_cell_id` e data na grade MERRA-2.
- Radiação SYN1DEG associada pelo centro mais próximo; distância máxima: `0,7071°`.
- Valores `-999` convertidos em nulos.
- ETo diária pela FAO-56 Penman–Monteith.
- Pressão estimada pela elevação; vapor real pela temperatura e umidade relativa médias.
- Fluxo de calor do solo diário igual a zero; `Rs/Rso` limitado de 0,3 a 1,0.
- Parquet por ano.
- Smoke test: 241 células × 243 datas = 58.563 linhas, sem nulos ou duplicidades.

```bash
uv run python -m water_stress.pipelines.run_transformation --source weather-daily
```

### `satellite_observation`

- Uma linha por `grid_id`, data, tile e item STAC.
- Filtro inicial por `soy_fraction > 0` e máscara final MapBiomas classe 39 no pixel.
- Escala `0,0001` e offset `-0,1` aplicados às reflectâncias L2A.
- Reflectâncias precisam ser finitas e estritamente positivas.
- B11 usa bilinear de 20 m para 10 m; SCL e MapBiomas usam vizinho mais próximo.
- SCL válidas: 4, 5, 6 e 7; nuvens: 8, 9 e 10.
- NDVI/NDMI com média, P10, P50 e P90.
- Percentis aproximados por histograma de 400 classes entre -1 e 1.
- Blocos de 512 × 512 pixels, sem raster intermediário persistido.
- Partição incremental e idempotente por item.
- O padrão mantém uma cena por mês e tile, prioriza tiles com maior quantidade estimada de células
  de soja cobertas e limita a 10 tiles por mês; `--tiles-per-month` ajusta esse limite,
  `--max-items` limita o lote da execução e `--coverage all` processa todos os itens do catálogo.
  A cota mensal considera partições completas já existentes e pula meses que já atingiram o limite.
  O índice das células de soja é pré-calculado uma vez por tile em cada execução e reutilizado entre
  cenas. A transformação usa até dois workers locais por padrão (`--workers`); a escrita permanece
  serializada e atômica.

Smoke tests reais:

- `S2B_21LWG_20230911_0_L2A`: 129 células com soja;
- `S2A_21LWG_20230926_0_L2A`: 38 células, NDVI médio de 0,287 a 0,873, sem
  duplicidades ou nulos;
- segunda execução do mesmo item retornou `reused`.

```bash
uv run python -m water_stress.pipelines.run_transformation --source satellite-observation
uv run python -m water_stress.pipelines.run_transformation \
  --source satellite-observation --max-items 5
uv run python -m water_stress.pipelines.run_transformation \
  --source satellite-observation --tiles-per-month 10 --workers 2 --max-items 30
uv run python -m water_stress.pipelines.run_transformation \
  --source satellite-observation --coverage all --max-items 20
uv run python -m water_stress.pipelines.run_transformation \
  --source satellite-observation --item-id S2A_21LWG_20230926_0_L2A
```

## Persistência PostgreSQL/PostGIS

O banco fica desabilitado por padrão. Configure `WATER_STRESS_DATABASE__...` em um `.env` local,
carregue as variáveis na sessão e execute:

```bash
set -a
source .env
set +a

uv run python -m water_stress.pipelines.run_database --migrate
uv run python -m water_stress.pipelines.run_database --register-bronze
uv run python -m water_stress.pipelines.run_database --load --dataset all
```

O PostgreSQL deve ter PostGIS instalado na mesma instância do servidor. O pgAdmin serve para
registrar a conexão e consultar as tabelas; ele não substitui o pipeline de carga. Uma nova
instalação ou porta gera um novo cluster, portanto o database precisa existir nessa instância.

### NASA POWER pontual legado

O transformador `--source nasa-power` do piloto municipal permanece para compatibilidade, mas a
tabela estadual vigente é `weather_daily`.

## Estrutura Silver

```text
data/silver/
├── dim_spatial_grid/state_code=51/resolution_meters=1000/
├── crop_mask/state_code=51/year=2023/resolution_meters=1000/
├── soil_features/state_code=51/resolution_meters=1000/
├── weather_daily/state_code=51/start_date=2023-09-01/end_date=2024-04-30/
└── satellite_observation/state_code=51/year={year}/month={month}/
    └── tile_id={tile}/item_id={item_id}/
```

Cada dataset possui schema e metadados. Relatórios de qualidade registram linhas, duplicidades,
nulos, intervalos, métodos espaciais e proveniência conforme aplicável.

## Notebooks

| Arquivo | Conteúdo |
|---|---|
| `notebooks/01_explore_ibge_boundary.ipynb` | Limite, extensão e metadados IBGE |
| `notebooks/02_explore_nasa_power_bronze.ipynb` | Estrutura e qualidade NASA POWER Bronze |
| `notebooks/02_explore_nasa_power.ipynb` | Exploração do Silver meteorológico legado |
| `notebooks/03_explore_soilgrids.ipynb` | Metadados, estatísticas e mapas SoilGrids |
| `notebooks/04_explore_sentinel_2.ipynb` | Catálogo, bandas, SCL e NDVI/NDMI |

Todos usam caminhos relativos. Os dados em `data/` permanecem locais.

## Comandos

```bash
uv sync --all-groups
uv run python -m water_stress.pipelines.run_ingestion --dry-run
uv run python -m water_stress.pipelines.run_ingestion
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run --group notebook jupyter lab notebooks/
```

Fontes individuais de ingestão: `ibge`, `nasa-power`, `soilgrids`, `sentinel-2` e `mapbiomas`.

## Qualidade confirmada

Após `satellite_observation`:

- 98 testes aprovados;
- cobertura total de 85,08%;
- Ruff e formatação aprovados;
- mypy estrito aprovado;
- smoke tests reais de todas as tabelas Silver estaduais;
- nenhum dado Bronze, Silver ou COG versionado.

## Atualização desta sessão — 2026-10-03

Foram implementadas otimizações locais para a transformação Sentinel-2:

- `sentinel_2.max_tiles_per_month` define 10 por padrão; a execução atual pode sobrescrever com
  `--tiles-per-month 10`;
- a cota mensal é cumulativa: Parquet e `_quality.json` completos já existentes contam para o mês;
  meses que atingiram a cota são pulados e meses parciais recebem somente os tiles restantes;
- a seleção prioriza a maior quantidade estimada de células de soja cobertas pelo footprint do tile,
  usando menor nebulosidade como desempate;
- o índice espacial de células candidatas é calculado uma vez por `tile_id` em memória e reutilizado
  nas cenas do mesmo tile;
- `sentinel_2.max_workers=2` limita o processamento local concorrente; `--workers 1` pode ser usado
  para reduzir concorrência quando a fonte remota estiver instável;
- o processamento de bandas pode ocorrer em paralelo, mas gravação de Parquet, qualidade e manifestos
  permanece serializada e atômica;
- logs registram candidatos, pendências, quantidade selecionada, tiles indexados e workers;
- não foi implementado cache persistente dos COGs; as bandas continuam sendo lidas remotamente;
- a ingestão Bronze não precisa ser repetida para usar essas melhorias.

Comando recomendado para retomar o processamento:

```bash
uv run python -m water_stress.pipelines.run_transformation \
  --source satellite-observation \
  --tiles-per-month 10 \
  --workers 2
```

As alterações desta sessão foram apenas locais e ainda não foram commitadas nem enviadas ao remoto.

## Limitações e decisões abertas

- O catálogo Sentinel-2 local possui 3.359 itens, distribuídos entre setembro de 2023 e abril de
  2024, e deve continuar incremental e monitorado.
- Podem existir múltiplos itens STAC para a mesma data e tile. A Gold usa a observação mais recente
  anterior ao fim da semana, limitada por `satellite_max_age_days=30`; mosaico ou prioridade entre
  itens da mesma data e tile ainda deve ser definido.
- Percentis Sentinel-2 são aproximações por histograma, com resolução de 0,005.
- A ETo usa umidade relativa média porque RH mínima/máxima não são ingeridas.
- INMET segue fora do escopo e poderá validar a meteorologia posteriormente.
- A grade adaptativa de 250 m para hotspots ainda não foi implementada.
- A persistência em S3/ADLS ainda não foi implementada; o contrato `StorageClient` prepara essa
  evolução para os arquivos e a conexão PostgreSQL permanece uma infraestrutura separada.

## Próximas etapas recomendadas

1. Definir mosaico/prioridade para itens Sentinel-2 da mesma data e tile.
2. Relacionar cada `grid_id` à célula `weather_cell_id` correspondente.
3. Calibrar o balanço hídrico, déficit e score com premissas agronômicas documentadas.
4. Criar testes espaço-temporais e notebooks de validação Gold.
5. Avaliar INMET ou outra fonte observacional para validação meteorológica.
6. Migrar o armazenamento de arquivos para S3/ADLS sem alterar as regras de negócio.

## Histórico de commits

| Commit | Entrega |
|---|---|
| `bcc35ba` | Pipeline inicial de ingestão Bronze |
| `d1a7fd9` | Ingestão SoilGrids e Sentinel-2 |
| `63d1b76` | NASA POWER pontual Bronze para Silver |
| `5fe24b4` | Notebooks Bronze e alinhamento Sentinel-2 |
| `078b9e5` | Arquitetura estadual e ingestões escaláveis |
| `acb7a6a` | Silver `crop_mask` |
| `73bf0d2` | Silver `soil_features` |
| `ddc5190` | Silver `weather_daily` e ETo |
| `065af5d` | Silver `satellite_observation` |

Estado desta atualização: seleção mensal Sentinel-2, cota cumulativa por mês, índice espacial
reutilizável, workers locais controlados, janela temporal Gold e documentação de persistência foram
atualizados localmente. As alterações desta atualização ainda não foram commitadas nem enviadas ao
remoto.

## Prompt para continuar no ChatGPT Web

```text
Estou desenvolvendo o repositório water-stress-analysis-data-pipeline. Use o arquivo
docs/first_session_context.md anexado como memória e considere também README.md e
docs/data_architecture.md.

O MVP atual cobre Mato Grosso (state_code=51) de 01/09/2023 a 30/04/2024. A Bronze de IBGE,
NASA POWER regional, SoilGrids, Sentinel-2 L2A e MapBiomas está implementada. A Silver estadual
possui dim_spatial_grid, crop_mask, soil_features, weather_daily e satellite_observation. A Gold
semanal, a persistência PostgreSQL/PostGIS, a seleção mensal limitada por tile e o índice espacial
reutilizável estão implementados. A próxima execução recomendada usa 10 tiles por mês e 2 workers;
as próximas decisões de produto são o mosaico/prioridade entre cenas concorrentes e a evolução do
armazenamento.

Antes de sugerir alterações, confirme o estado descrito nesta memória. Não presuma que dados locais
estejam versionados. Não faça commit nem push sem minha autorização explícita.
```

## Gold de consumo — atualização 2026-10-03

A Gold mantém o índice acadêmico ponderado vigente e agora distingue score completo, parcial e
indisponível. Meteorologia incompleta impede classificação; satélite ausente permite índice
parcial com cobertura de pesos explícita. A migration 002 cria a view de consumo para dashboard.
Partições têm checkpoints com checksums para restart seguro. Consulte o contrato de consumo
no guia de implementação. Calibração agronômica e validação observacional continuam pendentes.

Validação local da Gold de consumo: 4.839.624 linhas em 36 semanas, 134.434 células por semana;
1.107.000 scores completos e 3.732.624 parciais; nenhum indisponível nessa execução. Intervalo
observado [0, 1]. Qualidade global `warning` por valores ausentes nos atributos opcionais.
102 testes passaram, cobertura 85,30%, Ruff, formatação e mypy aprovados. A fixture de testes
agora isola também a raiz Gold em diretório temporário. A migration 002 ainda não foi aplicada
a uma instância PostgreSQL nesta sessão. Nenhum commit ou push foi realizado.

## Orquestração Airflow — atualização 2026-10-03

- `compose.airflow.yml` prepara Airflow 3.2.2/Python 3.12 em Docker Compose local, com
  LocalExecutor e PostgreSQL exclusivo para metadados; PostGIS existente continua separado.
- `dags/water_stress_pipeline.py` possui modos `full`, `satellite-gold` e `gold-only` (padrão),
  com publicação no banco opcional. O período permanece o configurado no YAML, sem avanço automático.
- O DAG começa pausado e manual, com uma execução/tarefa por vez. Não recebe `--force`.
- O gate `run_quality` bloqueia Gold vazia, relatório inválido ou qualidade `failed`;
  `warning` documentado permite publicação.
- Logs estruturados preservam operação, partição, resultado e contagem; transformação configura
  o logger na entrada CLI, sem alterações nas regras de negócio.
- 110 testes padrão aprovados, cobertura 85,73%, Ruff/formatação/mypy aprovados; 10 testes do DAG
  aprovados em ambiente temporário com Airflow real, incluindo serialização. Compose passou
  em `config --quiet`. A suíte padrão pula os testes do DAG quando Airflow não está instalado.
- Build e inicialização Docker ainda não executados: o daemon local estava desligado.
- Instalação, autenticação, disparo, recuperação e limites estão em `docs/airflow.md`.
- Não houve processamento de dados nem carga no PostGIS nesta etapa; nenhum commit/push.

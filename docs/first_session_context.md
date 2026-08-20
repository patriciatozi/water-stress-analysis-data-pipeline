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
- Gold, ainda não implementada, integrará somente os atributos necessários em janelas temporais.
- Dados baixados e produtos em `data/` não são versionados.
- Cada dataset registra fonte, extração, CRS, resolução, unidades e versão de processamento.

Tabelas atuais:

- `dim_spatial_grid`: dimensão espacial comum;
- `crop_mask`: fração anual de soja;
- `soil_features`: atributos estáticos de solo;
- `weather_daily`: meteorologia diária por célula NASA POWER;
- `satellite_observation`: índices espectrais por célula, cena e tile.

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
- O padrão processa a próxima cena pendente; `--max-items` controla o lote.

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
  --source satellite-observation --item-id S2A_21LWG_20230926_0_L2A
```

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

- 71 testes aprovados;
- cobertura total de 85,78%;
- Ruff e formatação aprovados;
- mypy estrito aprovado;
- smoke tests reais de todas as tabelas Silver estaduais;
- nenhum dado Bronze, Silver ou COG versionado.

## Limitações e decisões abertas

- O catálogo Sentinel-2 possui 3.128 itens e deve continuar incremental e monitorado.
- Podem existir múltiplos itens STAC para a mesma data e tile. A Gold deverá definir mosaico ou
  prioridade antes de criar uma observação temporal única por célula.
- Percentis Sentinel-2 são aproximações por histograma, com resolução de 0,005.
- A ETo usa umidade relativa média porque RH mínima/máxima não são ingeridas.
- INMET segue fora do escopo e poderá validar a meteorologia posteriormente.
- A grade adaptativa de 250 m para hotspots ainda não foi implementada.
- A camada Gold ainda não foi implementada.

## Próximas etapas recomendadas

1. Definir o contrato Gold e a janela temporal inicial de sete dias.
2. Definir mosaico/prioridade para itens Sentinel-2 da mesma data e tile.
3. Relacionar cada `grid_id` à célula `weather_cell_id` correspondente.
4. Construir features semanais de clima, ETo, chuva, solo, soja, NDVI e NDMI.
5. Definir balanço hídrico, déficit e score com premissas agronômicas documentadas.
6. Criar testes espaço-temporais e notebooks de validação Gold.
7. Avaliar INMET ou outra fonte observacional para validação meteorológica.

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

Estado desta atualização: `main` sincronizada com `origin/main` no commit `065af5d`.

## Prompt para continuar no ChatGPT Web

```text
Estou desenvolvendo o repositório water-stress-analysis-data-pipeline. Use o arquivo
docs/first_session_context.md anexado como memória e considere também README.md e
docs/data_architecture.md.

O MVP atual cobre Mato Grosso (state_code=51) de 01/09/2023 a 30/04/2024. A Bronze de IBGE,
NASA POWER regional, SoilGrids, Sentinel-2 L2A e MapBiomas está implementada. A Silver estadual
possui dim_spatial_grid, crop_mask, soil_features, weather_daily e satellite_observation. A próxima
etapa recomendada é definir a Gold semanal e a regra de mosaico/prioridade Sentinel-2.

Antes de sugerir alterações, confirme o estado descrito nesta memória. Não presuma que dados locais
estejam versionados. Não faça commit nem push sem minha autorização explícita.
```

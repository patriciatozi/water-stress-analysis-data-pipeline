# Memória de desenvolvimento — Water Stress Analysis Data Pipeline

> Documento portátil para retomar o projeto em outra sessão ou compartilhar o contexto com o
> ChatGPT Web. Atualize-o quando houver mudanças relevantes de escopo, arquitetura ou estado.

## Objetivo

Construir um pipeline acadêmico e reprodutível para integrar dados meteorológicos, de solo e de
sensoriamento remoto usados na análise espaço-temporal do risco de estresse hídrico da soja.

O MVP usa:

- município: Sorriso–MT;
- código IBGE: `5107925`;
- período: `2023-09-01` a `2024-04-30`;
- cultura de interesse: soja;
- execução local com preparação para migração futura a S3 ou ADLS.

Os resultados são destinados a estudo acadêmico e não constituem prescrição agronômica ou de
irrigação.

## Escopo implementado

### Camada Bronze

O pipeline preserva os arquivos recebidos das fontes e cria manifestos JSON contendo URL,
parâmetros, instante UTC, status HTTP, tamanho, SHA-256, município, versão do projeto e hash da
configuração.

Fontes implementadas:

1. **IBGE**
   - GeoJSON oficial do limite municipal de Sorriso–MT.
   - A geometria é usada para derivar o ponto da NASA POWER e as extensões espaciais das fontes
     geográficas.

2. **NASA POWER**
   - Série meteorológica diária consultada em um ponto representativo interno ao município.
   - Variáveis: `T2M`, `T2M_MAX`, `T2M_MIN`, `RH2M`, `WS2M`,
     `ALLSKY_SFC_SW_DWN` e `PRECTOTCORR`.

3. **SoilGrids**
   - Propriedades: argila (`clay`), areia (`sand`), carbono orgânico (`soc`) e densidade aparente
     (`bdod`).
   - Profundidades: `0-5cm`, `5-15cm` e `15-30cm`.
   - Estimativa mediana `Q0.5` via WCS, totalizando 12 GeoTIFFs.

4. **Sentinel-2 L2A**
   - Catálogo STAC Earth Search.
   - Cobertura máxima de nuvens configurada em 30%.
   - Seleção determinística de três cenas representativas em janelas temporais.
   - Ativos: `red`, `nir`, `swir16` e `scl`.
   - Downloads em streaming com checksum incremental.

A execução é idempotente. Arquivos íntegros da mesma requisição são reutilizados; `--force` cria
uma versão nova sem sobrescrever o Bronze anterior.

### Camada Silver

A transformação NASA POWER está concluída:

- uma linha por data;
- nomes de colunas em `snake_case`;
- latitude e longitude;
- unidades nos metadados do schema Parquet e em `_schema.json`;
- conversão do valor de preenchimento `-999` para `null`;
- validação de datas duplicadas;
- relatório `_quality.json` com nulos e contagens;
- Parquet compactado com Zstandard e particionado por ano.

O smoke test real gerou 243 datas, duas partições anuais e nenhum valor nulo ou duplicado.

Silver geoespacial e camada Gold ainda não foram implementadas.

## Decisões sobre Sentinel-2

As bandas possuem resoluções nativas diferentes:

- `red` e `nir`: 10 m;
- `swir16` e `scl`: 20 m.

Isso não é um erro da ingestão. A Bronze deve preservar as resoluções originais.

No notebook exploratório, as bandas contínuas são alinhadas apenas em memória à grade de 20 m da
SWIR16 com reamostragem bilinear. A leitura exploratória é limitada a 2048 pixels por eixo para
controlar memória. Esse procedimento permite calcular NDVI e NDMI para diagnóstico, mas não gera
um produto Silver.

Recomendação para a futura Silver:

- manter `red` e `nir` em 10 m para o NDVI;
- calcular NDMI em 20 m, reamostrando o NIR para a grade da SWIR16;
- usar interpolação bilinear para reflectância;
- usar vizinho mais próximo para `scl`, pois é categórica;
- aplicar a máscara SCL antes dos índices;
- recortar pelo limite municipal;
- registrar CRS, resolução, transformação, método de reamostragem e bandas de origem;
- não ampliar o NDMI para 10 m como se isso acrescentasse informação espacial.

A camada Gold deverá consumir índices já harmonizados pela Silver, sem repetir a reamostragem.

## Notebooks disponíveis

| Arquivo | Conteúdo |
|---|---|
| `notebooks/01_explore_ibge_boundary.ipynb` | Geometria, extensão, ponto representativo e manifesto IBGE Bronze |
| `notebooks/02_explore_nasa_power_bronze.ipynb` | Estrutura, unidades, qualidade e séries originais NASA POWER |
| `notebooks/02_explore_nasa_power.ipynb` | Schema, qualidade, chuva, temperatura e períodos secos da Silver |
| `notebooks/03_explore_soilgrids.ipynb` | Metadados, unidades, estatísticas e mapas dos rasters SoilGrids |
| `notebooks/04_explore_sentinel_2.ipynb` | Catálogo, COGs, SCL e diagnóstico de NDVI/NDMI com alinhamento de grades |

Todos usam caminhos relativos. Os dados em `data/` são locais e não são versionados.

## Comandos principais

Preparar o ambiente:

```bash
uv sync --all-groups
```

Planejar a ingestão sem download:

```bash
uv run python -m water_stress.pipelines.run_ingestion --dry-run
```

Executar todas as fontes ou uma fonte isolada:

```bash
uv run python -m water_stress.pipelines.run_ingestion
uv run python -m water_stress.pipelines.run_ingestion --source ibge
uv run python -m water_stress.pipelines.run_ingestion --source nasa-power
uv run python -m water_stress.pipelines.run_ingestion --source soilgrids
uv run python -m water_stress.pipelines.run_ingestion --source sentinel-2
```

Transformar NASA POWER de Bronze para Silver:

```bash
uv run python -m water_stress.pipelines.run_transformation --source nasa-power
```

Abrir os notebooks:

```bash
uv run --group notebook jupyter lab notebooks/
```

Validar o projeto:

```bash
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run mypy src tests
```

## Qualidade confirmada na última validação

- 41 testes aprovados;
- cobertura total de 85,22%;
- Ruff aprovado;
- verificação de formatação aprovada;
- mypy em modo estrito aprovado;
- notebooks executados sem erros;
- nenhum caminho absoluto nos notebooks.

## Estrutura técnica

- Python 3.12 e `uv`;
- layout `src/`;
- Pydantic Settings e YAML versionado;
- cliente HTTP compartilhado com timeout, retry e backoff;
- logging estruturado;
- abstração `StorageClient`, com implementação local;
- Ruff, mypy e pytest;
- pandas, matplotlib, JupyterLab e rasterio no grupo opcional `notebook`.

## Limitações atuais

- NASA POWER representa o município por um único ponto interno.
- SoilGrids ainda cobre o bounding box municipal, sem máscara pela geometria exata.
- COGs Sentinel-2 são preservados integralmente na Bronze.
- Máscara SCL, escala/offset, recorte, reprojeção e índices espectrais persistidos ainda não fazem
  parte da Silver.
- MapBiomas e INMET estão fora do escopo atual.
- Dados e segredos não devem ser enviados ao GitHub.

## Próximas etapas recomendadas

1. Implementar a Silver geoespacial do Sentinel-2, com máscara SCL, harmonização das grades,
   recorte municipal e NDVI/NDMI.
2. Implementar a Silver SoilGrids, com unidades físicas, máscara municipal e grade comum.
3. Definir uma grade analítica que permita integrar clima, solo e sensoriamento remoto.
4. Implementar ETo, ETc, balanço hídrico, déficit e score de risco com premissas documentadas.
5. Adicionar testes espaciais para CRS, alinhamento, resolução, nodata e validade dos índices.
6. Avaliar fonte observacional para validação meteorológica.

## Histórico relevante de commits

| Commit | Entrega |
|---|---|
| `bcc35ba` | Pipeline inicial de ingestão Bronze do MVP |
| `d1a7fd9` | Ingestão SoilGrids e Sentinel-2 |
| `ccd3b99` | README colaborativo expandido |
| `63d1b76` | Transformação NASA POWER Bronze para Silver |
| `7e84c32` | Notebook exploratório NASA POWER Silver |
| `5fe24b4` | Notebooks exploratórios Bronze e alinhamento Sentinel-2 |

Branch atual no momento da criação desta memória: `main`, sincronizada com `origin/main` no commit
`5fe24b4`.

## Prompt para continuar no ChatGPT Web

Copie o texto abaixo e anexe este arquivo, se possível:

```text
Estou desenvolvendo o repositório water-stress-analysis-data-pipeline. Use o arquivo
docs/first_session_context.md anexado como memória do projeto e considere o README do repositório como
documentação complementar.

O MVP estuda estresse hídrico da soja em Sorriso-MT, de 01/09/2023 a 30/04/2024. A ingestão Bronze
de IBGE, NASA POWER, SoilGrids e Sentinel-2 L2A está implementada. A Silver NASA POWER também está
pronta. A próxima decisão recomendada é implementar a Silver geoespacial, começando pelo
Sentinel-2, preservando as resoluções nativas na Bronze e harmonizando as bandas formalmente na
Silver.

Antes de sugerir alterações, confirme o estado atual descrito na memória. Não presuma que dados
locais estejam versionados. Não faça commit nem push sem minha autorização explícita.
```

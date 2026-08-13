# Water Stress Analysis Data Pipeline

Pipeline acadêmico e reprodutível para integrar dados meteorológicos, de solo e de sensoriamento remoto usados na análise espaço-temporal do risco de estresse hídrico da soja.

> Este README é uma documentação viva. Ele deve ser atualizado sempre que o escopo, as fontes, as decisões técnicas ou os comandos do projeto mudarem.

## Visão do projeto

O objetivo de longo prazo é produzir uma base espaço-temporal capaz de apoiar estudos de déficit hídrico e irrigação na agricultura de precisão. O projeto começa com uma área piloto em **Sorriso–MT** e evoluirá incrementalmente da ingestão de dados brutos até produtos analíticos.

Recorte atual:

- município: Sorriso–MT;
- código IBGE: `5107925`;
- período: `2023-09-01` a `2024-04-30`;
- cultura de interesse: soja;
- estágio atual: ingestão da camada Bronze.

As saídas deste repositório são estimativas acadêmicas. Elas não constituem prescrição agronômica ou operacional de irrigação.

## Estado atual

| Componente | Estado | Implementação |
|---|---|---|
| Limite municipal IBGE | Concluído e validado | GeoJSON original de Sorriso–MT |
| Meteorologia NASA POWER | Concluído e validado | JSON diário para um ponto representativo interno ao município |
| SoilGrids | Concluído e validado | 12 recortes GeoTIFF via WCS |
| Sentinel-2 L2A | Implementado | Catálogo STAC completo e download de quatro COGs para três cenas |
| Silver NASA POWER | Concluído e validado | Parquet diário particionado por ano, schema e relatório de qualidade |
| Silver geoespacial | Não iniciada | Recortes, harmonização espacial, unidades e qualidade |
| Camada Gold | Não iniciada | Integração espaço-temporal e indicadores hídricos |
| MapBiomas e INMET | Fora do escopo atual | Fontes candidatas para fases posteriores |

O smoke test do SoilGrids produziu 12 rasters válidos. A consulta real ao catálogo Sentinel-2 encontrou 131 itens e selecionou três cenas representativas; os COGs não são versionados no Git.

## Fontes ingeridas

### IBGE

O limite municipal oficial é a dependência espacial das demais fontes. A geometria original é preservada em GeoJSON e usada em memória para calcular o ponto da NASA POWER, o bounding box do SoilGrids e a interseção com cenas Sentinel-2.

### NASA POWER

Variáveis meteorológicas diárias:

- `T2M`, `T2M_MAX`, `T2M_MIN`;
- `RH2M`;
- `WS2M`;
- `ALLSKY_SFC_SW_DWN`;
- `PRECTOTCORR`.

### SoilGrids

Propriedades `clay`, `sand`, `soc` e `bdod`, nas profundidades `0-5cm`, `5-15cm` e `15-30cm`, usando a estimativa mediana `Q0.5`. Os recortes são solicitados ao WCS oficial no CRS Homolosine do SoilGrids e preservados como `GEOTIFF_INT16`.

### Sentinel-2 L2A

A busca usa o catálogo STAC Earth Search, cobertura de nuvens de até 30% e o período do estudo. O pipeline preserva o catálogo completo e divide o período em três janelas. Para cada janela, seleciona deterministicamente uma cena priorizando:

1. maior interseção com o município;
2. menor cobertura de nuvens;
3. data e identificador da cena como desempate.

Ativos baixados por cena: `red`, `nir`, `swir16` e `scl`. O download é feito em streaming, com checksum incremental, sem carregar o COG inteiro em memória.

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
│   └── sentinel_2.py
└── pipelines/
    └── run_ingestion.py        # CLI e ordem de execução
tests/                          # testes unitários e de integração simulada
data/bronze/                    # dados locais ignorados pelo Git
data/silver/                    # dados padronizados locais ignorados pelo Git
```

Fluxo atual:

```text
Configuração YAML
      ↓
Limite municipal IBGE
      ├──→ ponto representativo → NASA POWER
      ├──→ bounding box projetado → SoilGrids WCS
      └──→ geometria da busca → Sentinel-2 STAC → COGs selecionados
      ↓
Arquivos originais + manifestos na camada Bronze
      ↓
Transformação NASA POWER → validações → Parquet Silver por ano
```

## Organização da camada Bronze

```text
data/bronze/
├── ibge/municipality/municipality_code=5107925/
│   ├── municipality.geojson
│   └── municipality.manifest.json
├── nasa_power/daily/municipality_code=5107925/
│   └── start_date=2023-09-01/end_date=2024-04-30/
│       ├── weather.json
│       └── weather.manifest.json
├── soilgrids/municipality_code=5107925/
│   └── property={property}/depth={depth}/
│       ├── {property}_{depth}_Q0.5.tif
│       └── {property}_{depth}_Q0.5.manifest.json
└── sentinel_2/l2a/municipality_code=5107925/
    ├── start_date=2023-09-01/end_date=2024-04-30/
    │   ├── search-results.json
    │   └── search-results.manifest.json
    └── item_id={item_id}/
        ├── red.tif
        ├── nir.tif
        ├── swir16.tif
        ├── scl.tif
        └── *.manifest.json
```

Cada manifesto registra a origem, URL, parâmetros, instante UTC, status HTTP, tamanho, SHA-256, versão do projeto e hash da configuração. A execução padrão reutiliza somente arquivos cujo checksum e fingerprint da requisição continuam válidos. A opção `--force` cria uma versão imutável sem sobrescrever o artefato anterior.

## Camada Silver NASA POWER

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
data/silver/nasa_power/daily/municipality_code=5107925/
├── _schema.json
├── _quality.json
├── year=2023/part-000.parquet
└── year=2024/part-000.parquet
```

## Preparação do ambiente

Requisitos:

- Python 3.12;
- [uv](https://docs.astral.sh/uv/).

Instale as dependências:

```bash
uv sync --all-groups
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
```

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

- 41 testes aprovados;
- cobertura total superior a 85%;
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
- Os COGs Sentinel-2 são preservados integralmente. Máscara SCL, escala/offset, reprojeção, NDVI e NDMI ainda não são calculados.
- A NASA POWER representa o município por um único ponto interno; a comparação com estações INMET poderá ser incorporada na validação futura.
- O armazenamento atual é local, mas está isolado por `StorageClient` para futura implementação em S3 ou ADLS.
- Arquivos em `data/` não devem ser enviados ao GitHub.

## Próximas etapas sugeridas

1. Recortar e harmonizar os rasters SoilGrids por grade espacial.
2. Aplicar máscara SCL e calcular NDVI/NDMI para as cenas Sentinel-2.
3. Criar uma grade comum e integrar clima, solo e índices espectrais.
4. Implementar ETo, ETc, balanço hídrico, déficit e score de risco com premissas documentadas.
5. Adicionar notebooks exploratórios e validação com fontes observacionais.
6. Evoluir armazenamento e orquestração somente após estabilizar o MVP local.

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

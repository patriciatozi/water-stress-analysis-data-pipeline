# Water Stress Analysis Data Pipeline

Pipeline acadêmico para ingestão de dados públicos usados na análise espaço-temporal de estresse hídrico da soja.

Esta versão implementa a camada **Bronze** do MVP:

- limite municipal de Sorriso–MT (`5107925`) pelo IBGE;
- meteorologia diária da NASA POWER entre `2023-09-01` e `2024-04-30`;
- propriedades de solo SoilGrids em três profundidades;
- catálogo STAC Sentinel-2 L2A e quatro bandas de três cenas representativas;
- preservação das respostas originais e criação de manifestos de rastreabilidade.

MapBiomas, INMET, transformações Silver e produtos analíticos não fazem parte desta versão.

## Requisitos

- Python 3.12;
- [uv](https://docs.astral.sh/uv/).

## Instalação

```bash
uv sync --all-groups
```

## Execução

Executar todas as fontes, respeitando a dependência do limite do IBGE:

```bash
uv run python -m water_stress.pipelines.run_ingestion
```

Executar uma única fonte:

```bash
uv run python -m water_stress.pipelines.run_ingestion --source ibge
uv run python -m water_stress.pipelines.run_ingestion --source nasa-power
uv run python -m water_stress.pipelines.run_ingestion --source soilgrids
uv run python -m water_stress.pipelines.run_ingestion --source sentinel-2
```

Visualizar o plano sem acessar as APIs ou gravar arquivos:

```bash
uv run python -m water_stress.pipelines.run_ingestion --dry-run
```

Criar uma nova versão imutável mesmo quando já existe um artefato íntegro:

```bash
uv run python -m water_stress.pipelines.run_ingestion --force
```

Use `--config caminho/project.yml` para selecionar outro arquivo de configuração.

## Estrutura Bronze

```text
data/bronze/
├── ibge/municipality/municipality_code=5107925/
│   ├── municipality.geojson
│   └── municipality.manifest.json
├── nasa_power/daily/municipality_code=5107925/
    └── start_date=2023-09-01/end_date=2024-04-30/
        ├── weather.json
        └── weather.manifest.json
├── soilgrids/municipality_code=5107925/
│   └── property={property}/depth={depth}/
│       └── {property}_{depth}_Q0.5.tif
└── sentinel_2/l2a/municipality_code=5107925/
    ├── start_date=2023-09-01/end_date=2024-04-30/search-results.json
    └── item_id={item_id}/{red,nir,swir16,scl}.tif
```

Cada manifesto registra fonte, URL, parâmetros, instante UTC, código HTTP, tamanho, SHA-256, município, período e hash da configuração. Uma execução comum reutiliza um artefato somente quando seu checksum ainda é válido. `--force` cria arquivos versionados e nunca sobrescreve o Bronze existente.

Os dados baixados são ignorados pelo Git. Apenas os scripts, configurações e arquivos `.gitkeep` são versionados.

## Qualidade

```bash
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run mypy src tests
```

## Configuração

A configuração padrão está em `configs/project.yml`. Variáveis de ambiente com prefixo `WATER_STRESS_` podem substituir configurações simples, como `WATER_STRESS_STORAGE__ROOT_PATH`.

O acesso a arquivos é isolado pela interface `StorageClient`. A implementação atual usa disco local; uma migração futura pode adicionar adaptadores S3 ou ADLS sem alterar os clientes de ingestão.

## Licenciamento e fontes externas

O código-fonte está sob a [MIT License](LICENSE). Os dados não são redistribuídos pelo repositório e permanecem sujeitos às condições de seus provedores:

- [IBGE – API de Malhas](https://servicodados.ibge.gov.br/api/docs/malhas?versao=3);
- [NASA POWER](https://power.larc.nasa.gov/docs/services/api/temporal/daily/).
- [ISRIC SoilGrids WCS](https://docs.isric.org/globaldata/soilgrids/wcs.html);
- [Element 84 Earth Search](https://earth-search.aws.element84.com/v1).

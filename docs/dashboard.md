# Dashboard Streamlit

O dashboard explora o **score provisório v1** e os fatores já publicados na Gold. O balanço
superficial v2 permanece separado, sem integrar este painel. A aplicação apenas lê dados:
não executa ingestão, transformação, migrations ou carga no PostgreSQL.

## Executar

Na raiz do repositório:

```bash
uv sync --group dashboard
uv run --group dashboard streamlit run dashboard_app.py
```

Abra `http://localhost:8501`. As dependências visuais ficam no grupo opcional `dashboard`,
sem serem exigidas pelos CLIs do pipeline. Para usar outra configuração:

```bash
uv run --group dashboard streamlit run dashboard_app.py -- --config configs/project.yml
```

## Executar no Docker

O serviço `dashboard` de `compose.airflow.yml` usa a mesma imagem `water-stress-pipeline:local`
do Airflow, com as dependências visuais e o tema incluídos. Cada serviço tem seu próprio processo;
iniciar somente o dashboard não inicia o Airflow nem o banco de metadados.

Prepare `.env.airflow` conforme o [guia de Airflow](airflow.md#primeiro-início), preservando o
arquivo existente. As credenciais do PostGIS vêm de `.env`, carregado pelo Compose em tempo
de execução, sem ser copiado para a imagem. `AIRFLOW_PIPELINE_DATABASE_HOST` é usado pelos
dois serviços; `host.docker.internal` alcança o banco da máquina no Docker Desktop.

Para iniciar somente o dashboard:

```bash
docker compose --env-file .env.airflow -f compose.airflow.yml config --quiet
docker compose --env-file .env.airflow -f compose.airflow.yml up -d --build dashboard
docker compose --env-file .env.airflow -f compose.airflow.yml ps dashboard
```

Abra `http://localhost:8501`. Encerre antes um Streamlit local que esteja usando essa porta.
Para iniciar todos os serviços, use `up -d --build` sem o nome `dashboard`.

```bash
docker compose --env-file .env.airflow -f compose.airflow.yml logs --tail 100 dashboard
docker compose --env-file .env.airflow -f compose.airflow.yml stop dashboard
```

No modo local, o dashboard monta somente `configs/`, para leitura; não precisa montar os dados Bronze,
Silver ou Gold. Alterações na interface, tema, código ou dependências exigem reconstruir a
imagem e recriar o serviço com `up -d --build dashboard`. O healthcheck verifica o servidor
Streamlit; a disponibilidade dos dados é informada na própria interface.

## Leitura do PostgreSQL

Por padrão (`WATER_STRESS_DASHBOARD__SOURCE=postgres`), o dashboard lê os indicadores pela
view `gold.water_stress_dashboard` no PostgreSQL,
no período e na resolução configurados. Não há seletor de fonte nem leitura alternativa de
arquivos locais quando a conexão falha. São necessários banco habilitado, migrations 001–003
e carga da Gold v1. As consultas são parametrizadas, em transações somente de leitura,
com timeout de 30 s por consulta.

O dashboard carrega o `.env` da raiz automaticamente, preservando variáveis já definidas no
ambiente. Para PostgreSQL, configure `WATER_STRESS_DATABASE__ENABLED=true` e as credenciais
documentadas no [guia de implementação](implementation_guide.md). Erros de conexão exibem uma
mensagem sem credenciais. A conexão é feita pelo servidor Streamlit, não pelo navegador.

**Não é necessário TRUNCATE, nova migration nem reprocessamento para instalar o dashboard.**
Se a Gold já possui o contrato atual, basta iniciar a aplicação. Dados antigos sem classificação
não entram no mapa/distribuição por classe; a interface não infere uma nova classe. A atualização usa
os comandos Gold existentes, descritos no guia de implementação.

## Leitura do S3 na AWS

A fonte é escolhida por configuração de ambiente, sem seletor na interface. Na AWS, definir:

```dotenv
WATER_STRESS_DASHBOARD__SOURCE=s3
WATER_STRESS_DASHBOARD__LOCATION__BUCKET=SEU_BUCKET
WATER_STRESS_DASHBOARD__LOCATION__PREFIX=water-stress/dashboard
WATER_STRESS_DASHBOARD__LOCATION__REGION=us-east-1
```

O painel lê `current.json`, baixa a grade Silver e a Gold v1 para `.dashboard-cache/`, verifica
SHA-256/tamanhos e só promove o diretório depois de todos os arquivos estarem completos.
Os arquivos publicados preservam os schemas, unidades, CRS, grãos, partições e metadados dos
datasets originais. A associação por `grid_id` e os resumos são os mesmos do leitor Parquet.
Não consulta PostgreSQL nem os arquivos de processamento da EC2 nesse modo.

O cache é persistente em um volume Docker separado. Downloads interrompidos retomam arquivos
já verificados; revisões completas são reutilizadas. O ponteiro S3 é consultado por até 60 s
de cache da aplicação; “Atualizar leitura” força a consulta imediatamente. A role da EC2 acessa
o bucket privado via SDK, sem credenciais copiadas para a imagem. Veja [AWS](aws_deployment.md).

## Navegação e indicadores

- **Visão geral:** score médio, área em alto/crítico, cobertura de score completo, explicação
  destacada do score, mapa com detalhes, distribuição por classe, composição e fatores da semana.
- **Sobre o indicador:** premissas, classes, parâmetros e limitações do método.

Em **Como o score é composto?**, ETo, NDVI e NDMI têm explicações curtas ao passar o mouse ou
focar as siglas com o teclado.

A seleção semanal no modo PostgreSQL consulta `gold.water_stress_weekly`, com o mesmo filtro de período e grade.
A chave estrangeira da Gold para a dimensão espacial garante as mesmas semanas da view; listar
datas não precisa repetir o join espacial de milhões de linhas. Os indicadores continuam vindo
da view, em consultas somente de leitura.

A seleção semanal usa somente semanas disponíveis na fonte selecionada. O intervalo de datas apresentado respeita
as bordas do estudo. Os valores ausentes permanecem nulos, sem interpolação.

O mapa usa centróides em EPSG:4326. O filtro **Categorias** reúne as classes
(verde) e a completude Completo/Parcial (amarelo). Classes selecionadas se combinam com a
completude selecionada; sem escolha de completude, todas as condições são exibidas. Escolher
apenas uma completude exibe todas as classes nessa condição. O mapa, sua legenda de áreas e o
gráfico **Área por classe** usam exatamente as mesmas células filtradas. Sem score e classificação
pendente são excluídos desses elementos, inclusive quando só há filtro de completude. Os cartões
superiores, a composição e os fatores continuam representando a semana selecionada.
Os marcadores representam centróides da grade analítica, não polígonos de talhões.
Áreas são as publicadas na dimensão espacial: o painel não reprojeta,
reamostra ou recalcula geometrias.

## Cálculos e unidades

O painel recebe o score 0–1 e a classificação da Gold e apresenta o score em 0–100. Não
recalcula a regra de negócio. O método `academic-index-v1` combina déficit hídrico, NDVI e
NDMI; os pesos padrão são 50/30/20, provisórios. Sem índices espectrais válidos, usa os pesos
disponíveis renormalizados; sem clima completo, o score fica indisponível. ETc, chuva efetiva
e retenção do solo não participam desse score. As propriedades do solo aparecem como contexto.

| Medida | Regra de apresentação |
|---|---|
| Área equivalente de soja | `area_km2 * soy_fraction`, em km² |
| Score médio | `sum(score * área) / sum(área)` entre células com score válido |
| Área com score completo | Área com status `complete` / área total de soja no recorte, em % |
| Área em alto/crítico | Soma da área das células classificadas nessas classes; sem classificação disponível, exibe ausência |
| Média de cada fator | Ponderada pela área com aquele fator disponível, acompanhada de sua própria cobertura |

Zero é um valor válido. Células sem classificação publicada não entram no mapa nem no gráfico
por classe, sem descartar valores numéricos existentes dos outros indicadores. A composição
mostra as áreas Completo e Parcial da semana: completo tem os três fatores do score; parcial
usa parte deles. A disponibilidade de componentes não afirma validação agronômica.

| Fator | Unidade | Interpretação |
|---|---|---|
| Chuva | mm na janela semanal | Precipitação acumulada |
| ETo | mm na janela semanal | Evapotranspiração de referência acumulada |
| Déficit hídrico | mm na janela semanal | `max(0, ETo - chuva)` publicado pela Gold |
| NDVI e NDMI | Índices −1 a 1 | Medianas espectrais publicadas na Gold, sujeitas à idade máxima configurada |
| Temperatura | °C | Média na janela |
| Sequência seca | dias | Sequência publicada pela Gold |
| Argila e areia | % | Propriedades de 0–30 cm |
| Carbono orgânico | g/kg | Propriedade de 0–30 cm |
| Densidade do solo | g/cm³ | Propriedade de 0–30 cm |

Na seção Sobre o indicador, PostgreSQL mostra os parâmetros da configuração atual: a view
não guarda todos os pesos/limiares de cada execução. No modo S3, os parâmetros e o timestamp
de processamento vêm dos metadados Gold publicados junto aos arquivos.

## Processamento e limites

A aplicação consulta apenas as colunas necessárias e mantém uma semana por leitura. Resultados
visuais usam cache por até cinco minutos. Use “Atualizar leitura” após uma recarga do banco
para consultar os dados atualizados imediatamente.

O mapa mostra todas as células selecionadas, sem amostragem. A base cartográfica CARTO fica
sempre habilitada e requer internet.
Essa base é apoio visual, não uma fonte analítica. A aplicação não consulta NASA POWER,
Sentinel-2, SoilGrids, MapBiomas ou IBGE.

A máscara anual não informa plantio ativo em cada semana. A meteorologia tem resolução mais
grossa que a grade analítica; os índices espectrais não medem diretamente água no solo. O
painel é exploratório e acadêmico, sem prescrição automática de irrigação.

## Verificação

```bash
uv run --group dashboard pytest
uv run ruff check .
uv run ruff format --check .
uv run mypy src
```

Os testes usam fixtures sintéticas e mocks, incluindo leitura somente de leitura no banco,
ponderação/cobertura, nulos, chaves inválidas, filtros e navegação com `AppTest`. Os testes de
interface são pulados quando o grupo `dashboard` não está instalado. A suíte padrão não
depende de acesso de rede nem de dados do estudo.

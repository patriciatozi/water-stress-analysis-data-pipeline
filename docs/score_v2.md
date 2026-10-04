# Score v2 com reservatório superficial

Implementado em 2026-10-04, como indicador acadêmico de cenário, sem validação de campo.
Método `academic-index-v2-surface-30cm`; retenção `saxton-rawls-2006-fc-wp-v1`;
classificação `four-level-v1`. O método v1, seus arquivos, tabela e view continuam disponíveis.

## Entradas e parâmetros

O comando `gold-water-balance` lê o clima diário Silver e reutiliza solo, associação meteorológica,
fração de soja, índices selecionados e score v1 dos Parquet Gold já existentes. Não faz requisições,
download, leitura de COGs, reprojeção ou transformação Sentinel-2. Exige células na mesma ordem e
com atributos estáticos consistentes entre semanas; entradas incompatíveis precisam de regeneração
da Gold v1. A grade mantém EPSG:5880 e resolução configurada de 1 km.

Parâmetros em `water_balance` de `configs/project.yml`:

| Parâmetro | Valor do piloto | Interpretação |
|---|---|---|
| Plantio | 15/10/2023 | Hipótese confirmada pelo usuário; não observado |
| Durações inicial/desenvolvimento/meio/final | 20/30/60/30 dias | Ciclo de 140 dias, termina em 02/03/2024 |
| Kc inicial/meio/final | 0,40/1,15/0,50 | Referência bibliográfica; rampas lineares no desenvolvimento e final |
| Profundidade | 0,30 m, fixa | Camada homogênea aproximada a partir das médias existentes |
| Fração de depleção `p` | 0,50 | Hipótese constante, sem ajuste diário pela ETc |
| Conversão carbono → matéria orgânica | 1,724 | Hipótese convencional, não uma medição local |
| Água disponível inicial | 25%, 50%, 75% | Cenários, não intervalo de confiança |
| Escoamento | 0% da chuva | Referência simplificada, configurável |
| Bloco espacial | 2.048 células | Limite de processamento; três cenários compartilham entradas |

Kc segue a tabela de referência da [FAO-56, capítulo 6](https://www.fao.org/4/x0490e/x0490e0b.htm).
O modelo usa coeficiente único de cultura e reservatório simplificado; não implementa integralmente
FAO-56. Não corrige densidade, cascalho ou salinidade; não simula irrigação, ascensão capilar,
evaporação fora do ciclo ou raízes abaixo de 30 cm. `bulk_density` permanece no dataset original.
O armazenamento superficial não equivale à água disponível em toda a zona radicular da soja.

## Retenção e balanço

Para Saxton–Rawls, `S=areia_pct/100`, `C=argila_pct/100`, `OM=soc_g_kg/10 × 1,724` (%).
Aplicam-se as equações 1 e 2 do [artigo original](https://scispace.com/pdf/soil-water-characteristic-estimates-by-texture-and-organic-56ntsbkyo4.pdf):

```text
wp_t = -0,024S + 0,487C + 0,006OM + 0,005S·OM - 0,013C·OM + 0,068S·C + 0,031
theta_wp = 1,14wp_t - 0,02
fc_t = -0,251S + 0,195C + 0,011OM + 0,006S·OM - 0,027C·OM + 0,452S·C + 0,299
theta_fc = fc_t + 1,283fc_t² - 0,374fc_t - 0,015
TAW = 1000 × (theta_fc - theta_wp) × 0,30
```

Umidades volumétricas: m³/m³; TAW: mm. Aplicar equações não lineares às médias da camada é uma
aproximação. Textura finita, não negativa e soma areia+argila ≤100%; OM entre 0 e 8% é a política
conservadora para este modelo mineral. Exigir `0 ≤ theta_wp < theta_fc ≤ 1`. Valores ausentes ou
inválidos geram solo inválido, motivo e score indisponível; nenhum valor é ajustado silenciosamente.

O cálculo diário usa `A` como água disponível acima do ponto de murcha:

```text
A0 = fração inicial × TAW
P_net = chuva × (1 - fração de escoamento)
A_temp = A_anterior + P_net
Ks = min(1, min(TAW, A_temp) / ((1 - p) × TAW))
ETc_pot = Kc × ETo
ETc_adj = min(A_temp, Ks × ETc_pot)
DP = max(0, A_temp - ETc_adj - TAW)
A_final = A_temp - ETc_adj - DP
chuva_aproveitada = P_net - DP
estresse = 1 - Ks
```

A ordem chuva → consumo → drenagem é a convenção diária do projeto. Chuva aproveitada é uma
estimativa, não chuva efetiva medida. Conservação: `A_final + ETc_adj + DP = A_anterior + P_net`.
O [capítulo 8 da FAO](https://www.fao.org/4/x0490e/x0490e0e.htm) fundamenta Ks e disponibilidade;
a implementação limita também o consumo à água existente e usa profundidade superficial fixa.

Inicializar no plantio. Se o estudo começar depois do plantio e ainda dentro do ciclo, o comando
recusa a execução: a condição inicial não pode ser atribuída a uma data diferente silenciosamente.
Sem chuva ou ETo em qualquer dia ativo, invalidar a reserva desse dia e de todo o restante do
ciclo. Não preencher zeros, carregar o estado anterior ou reiniciar a reserva na semana seguinte.

## Agregação, score e contrato

Semana de segunda a domingo, incluindo semanas parciais. `active_day_count` conta somente dias
ativos esperados; `valid_day_count` conta dias simulados antes de eventual lacuna. Produzir média
e máximo de `1-Ks` apenas com todos os dias ativos válidos. ETc, chuva aproveitada e drenagem são
somas desses dias; água inicial/final são estados em mm. Ausência invalida os agregados da semana.

Persistência é diagnóstica: dias com Ks<1, maior sequência **dentro da semana** e sequência em
andamento ao final, que pode ultrapassar sete dias. Não acrescenta peso ao score.
`crop_stage` representa a fase do último dia ativo da semana; Kc varia diariamente.

```text
score_v2 = 0,50 × estresse_hídrico_médio + 0,30 × estresse_NDVI + 0,20 × estresse_NDMI
estresse_índice = limitar((limiar - índice) / max(abs(limiar), 10⁻⁹), entre 0 e 1)
```

Reutilizam-se os limiares v1 (NDVI 0,7; NDMI 0,2) e idade máxima de 30 dias. Os pesos de `gold`
são compartilhados; `gold.deficit_weight` passa a ponderar o componente hídrico v2 e deve ser
positivo. Os pesos são provisórios. Classes: ≤0,25 baixo; ≤0,50 atenção; ≤0,75 alto; acima crítico.
A mesma classe não representa equivalência agronômica entre v1 e v2.

| Status | Política |
|---|---|
| `complete` | Todos os componentes de peso positivo presentes |
| `partial` | Componente hídrico obrigatório válido; renormalizar pesos dos índices presentes |
| `unavailable` | Falta componente hídrico: score/classe nulos e motivo explícito |
| `not_applicable` | Sem dias do ciclo na semana: score/classe nulos, distinto de ausência |

`score_available_weight` registra fração do peso disponível; `score_component_count`, quantidade
de componentes. Zero é score válido. Idade e índices disponíveis permanecem na saída para auditoria.

| Dataset/tabela Gold | Chave | Conteúdo |
|---|---|---|
| `soil_hydraulics` | `analysis_id, grid_id` | Umidades, TAW, OM, método, status/motivo |
| `water_stress_weekly_v2` | `analysis_id, grid_id, week_start, scenario_id` | Balanço, persistência, score v1/v2, satélite, classe, cobertura e motivos |

Schemas Arrow com unidades estão em `_schema.json`. Manifestos registram timestamp, parâmetros,
fontes com SHA-256, CRS, resolução, versões, escopo e pressupostos. `analysis_id` identifica estudo,
resolução e escopo (`full` ou `pilot-N`); parâmetros e entradas compõem `model_signature`.
Mudanças de parâmetros mantêm o identificador da análise e substituem sua publicação atual.

Partições: `water_balance/{estudo}/start_date=.../end_date=.../scope=.../runs/{assinatura}/`
`block_id=.../scenario_id=.../week_start=.../part-000.parquet`.
Solo é publicado uma vez por bloco. Checkpoints encadeados armazenam reserva e sequência, conferem
checksums e invalidam as semanas seguintes do bloco/cenário após interrupção ou corrupção.
Escritas individuais são atômicas; `_metadata.json` publica ao final apenas os arquivos completos.
O carregador usa esse manifesto, nunca glob de todos os runs/checkpoints. Runs antigos ficam em
disco; não removê-los durante processamento/carga. Não existe limpeza automática nesta entrega.

## Executar e carregar

O piloto técnico utiliza as primeiras N células na ordem existente da Gold, de forma determinística;
não é amostra representativa do estado. O piloto de 32 células já foi gerado localmente:

```bash
uv run python -m water_stress.pipelines.run_transformation --source gold-water-balance --max-cells 32
uv run python -m water_stress.pipelines.run_quality --dataset water_stress_weekly_v2 --max-cells 32
```

Para carregar **somente o piloto**, após habilitar a configuração PostgreSQL na mesma sessão:

```bash
set -a
source .env
set +a
uv run python -m water_stress.pipelines.run_database --migrate
uv run python -m water_stress.pipelines.run_database --load --dataset soil_hydraulics --dataset water_stress_weekly_v2 --max-cells 32
```

A dimensão `silver.dim_spatial_grid` deve estar carregada. Se faltar, carregar o Parquet local com
`--load --dataset dim_spatial_grid`; isso não ingere novas fontes. A migration
`004_surface_water_balance.sql` cria as duas tabelas e `gold.water_stress_dashboard_v2`.
Ela não converte, limpa ou substitui as tabelas/view v1. O PostgreSQL do projeto não foi alterado.

**Nenhum TRUNCATE é necessário.** A carga usa COPY em staging e UPSERT, seguido de remoção de
chaves ausentes **somente nos `analysis_id` carregados**, na mesma transação. Assim, mudanças de
cenários/células removem linhas obsoletas da análise sem afetar piloto, outros estudos ou v1.
Uma carga vazia é recusada e falhas revertem o dataset. A atomicidade é por dataset, não pelas
duas tabelas em conjunto; repetir ambos os carregamentos após falha. A view só associa propriedades
hidráulicas de assinatura compatível. `--dataset all` mantém compatibilidade sem publicação v2;
quando existe publicação `full`, inclui as duas tabelas novas. Para pilotos use nomes explícitos.

Após revisar o piloto e o custo, gerar toda a área retirando `--max-cells`. A carga e o gate também
devem retirar essa opção; o escopo full é distinto do piloto. Airflow oferece `water-balance-only`
para esse processamento local completo, sem ingestão ou transformação de satélite. Reconstruir
a imagem para incorporar código novo. A execução estadual ainda não foi realizada nesta entrega.

No dashboard, selecionar **um `analysis_id`, uma semana e um cenário**. Não somar áreas ou misturar
linhas de cenários/piloto/full. Exibir camada de 30 cm, calendário de cenário, status, cobertura,
idade do satélite e método. Filtrar `scenario_id='initial-0.5'` para a referência de 50%.

## Piloto técnico e verificações

Em 32 células × 36 semanas × 3 cenários: 3.456 linhas, 1.440 não aplicáveis, 1.953 parciais e
63 indisponíveis. Solo válido em 31 células; uma sem areia, argila ou carbono. Nenhum score completo
nesta amostra, por ausência de satélite válido. Cada cenário tem 651 scores: 437 baixos, 152 atenção
e 62 altos. Médias simples nos mesmos 651 pares do cenário 50%: v1=0,480186; v2=0,150186.
Essas médias descrevem o piloto, não o risco estadual nem melhora de acurácia.

Os três cenários deram scores iguais nesta amostra; a chuva eliminou a diferença inicial antes
do cálculo de estresse. Isso não prova irrelevância da condição inicial em outras células/períodos.
Os testes secos sintéticos verificam sensibilidade e persistência. Medição de retomada neste
Mac: aproximadamente 0,82 s e pico RSS de 542 MiB, incluindo Python/Arrow e buffers Parquet.
O tamanho de lote limita os estados de cálculo, mas leitores retêm buffers de row groups de entrada.
Não extrapolar diretamente esse tempo para todo o estado.

Testes sintéticos verificam unidades, conservação, fases, semanas parciais, dados inválidos,
lacunas, scores completos/parciais, classificação, isolamento, reuso e reparo em cadeia.
Airflow foi testado com suas classes reais. Em PostgreSQL 17/PostGIS temporário foram verificadas
migrations 001–004, reaplicação, carga do piloto, idempotência, remoção de cenário obsoleto, preservação
de outra análise e do v1, view e rollback com score inválido. Esse servidor foi encerrado.
Verificação final: 173 testes padrão aprovados, 1 módulo Airflow ignorado no ambiente padrão,
cobertura de 89,15%; 14 testes Airflow aprovados no ambiente separado. Ruff, formatação e mypy
aprovados. Nenhum commit ou push realizado.

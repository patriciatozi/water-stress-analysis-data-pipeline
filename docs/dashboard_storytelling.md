# Decisões e leitura do dashboard

Atualização: 10/10/2026. Público confirmado: público geral e banca acadêmica. Pergunta principal:
**Como os sinais de risco mudaram ao longo de todo o período?**

## Contexto recuperado de docs/

| Decisão vigente | Consequência para o painel | Referência |
|---|---|---|
| Mato Grosso, set/2023 a abr/2024, grade analítica de 1 km | Mostrar região e período junto ao título; pontos do mapa não são talhões | [Arquitetura](data_architecture.md) |
| Manter `academic-index-v1` no dashboard | Apresentar indicador provisório, sem prometer recomendação de irrigação | [Memória, decisão do dashboard](first_session_context.md#dashboard-streamlit--2026-10-04) |
| Balanço superficial v2 implementado como piloto, separado do painel | Não misturar métodos nem cenários com o histórico estadual v1 | [Score v2](score_v2.md) e [plano](score_v2_implementation_plan.md) |
| Área equivalente = área da célula × fração de soja | Ponderar resumos por área; não contar todas as células como 1 km² de soja | [Guia de implementação](implementation_guide.md#gold-de-consumo--índice-acadêmico-v1-contrato-v2) |
| Classes publicadas na Gold; ausência não é baixo risco | Usar classes existentes; não classificar a média mensal; preservar lacunas | [Dashboard](dashboard.md#cálculos-e-unidades) |
| Componentes incompletos têm pesos renormalizados | Mostrar disponibilidade junto à evolução; não apresentar completude como precisão | [Guia de implementação](implementation_guide.md#gold-de-consumo--índice-acadêmico-v1-contrato-v2) |
| PostgreSQL local; publicação S3 com cache na AWS | Reutilizar os leitores existentes e dados publicados | [AWS](aws_deployment.md#publicação-s3-e-cache-do-dashboard) |
| Painel somente de leitura | Melhorar apresentação sem executar pipeline, migrations ou carga | [Dashboard](dashboard.md) e [Airflow](airflow.md) |

A memória é histórica: suas seções antigas citam leitores e navegações já substituídos.
As seções de 10/10/2026 e os guias atuais prevalecem para o modo AWS/S3.

## Aplicação de Storytelling com Dados

| Conceito | Aplicação |
|---|---|
| Entender o contexto e o público | Primeira tela responde à evolução do estudo, sem exigir conhecimento de siglas |
| Escolher a representação | Linha mensal para comparar no tempo; barras para disponibilidade; mapa em uma visão semanal |
| Reduzir elementos | Dois gráficos principais; tabela, fatores, histórico e detalhes em seções expansíveis |
| Direcionar a atenção | Azul escuro destaca o indicador; disponibilidade usa cinza; rótulos diretos nos valores mensais |
| Construir uma narrativa | Primeiro a conclusão observada, depois evolução e disponibilidade, depois exploração e método |
| Explicar sem exagerar | “Indicador”, “vigor” e “umidade”; escala 0–100 não é probabilidade de perda |

Referências da própria autora:
[contexto do público](https://www.storytellingwithdata.com/blog/2015/06/audience-audience-audience),
[reduzir elementos e destacar o essencial](https://www.storytellingwithdata.com/blog/two-tips-for-better-graphs).

## Critérios da comparação mensal

Todos os meses do estudo aparecem juntos. Médias ponderam a área de soja e os dias da semana
que pertencem ao mês. A mesma área não é acumulada como hectares novos. Semanas ausentes não
recebem zero; meses incompletos mostram sua disponibilidade temporal. O resumo descreve sinais
observados no indicador acadêmico, sem inferir perdas, causas comprovadas ou irrigação necessária.
O [contrato mensal](dashboard.md#resumo-mensal-contrato-de-apresentação) registra fórmulas e nulos.

## Verificação desta atualização

- Resumos conferidos nos Parquet locais existentes: 36 semanas e oito meses, set/2023–abr/2024,
  com todas as datas do estudo representadas. Primeira agregação medida em cerca de 22 s nesta
  máquina; não é garantia de tempo na EC2 ou no PostgreSQL.
- AppTest com dados locais reais validou visão do período, tabela de oito meses, última semana,
  mapa, fatores e método. Testes sintéticos verificam ponderação, lacunas, ausência, filtros,
  fontes PostgreSQL/S3 e navegação. Suíte: 250 testes aprovados, um módulo Airflow ignorado no
  ambiente padrão; Ruff, formatação e mypy aprovados.
- A conexão com o navegador de inspeção ficou indisponível; revisão visual no navegador pendente.
  Não foi feita publicação na AWS. A atualização da imagem segue os comandos do guia do dashboard.

## Identidade visual — 11/10/2026

A pedido do usuário, a paleta principal passou para azul escuro, com área de leitura clara.
A barra lateral reúne logo branco do PECE, nome completo
“Estresse Hídrico e Recomendação de Irrigação na Soja” e, abaixo, “Explorar os dados”.
As cores das classes de risco conservam seu significado. Origem do logo em
[assets/README.md](../assets/README.md); regras de tema em [dashboard.md](dashboard.md#identidade-visual).

Verificação: 251 testes aprovados, um módulo Airflow ignorado; Ruff, formatação e mypy aprovados.
Imagem Docker reconstruída com os assets; logo, contraste, ordem dos elementos e gráficos
conferidos em uma prévia com os Parquet locais. Prévia temporária encerrada.

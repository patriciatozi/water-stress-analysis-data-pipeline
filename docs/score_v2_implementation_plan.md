# Plano de implementação do score com balanço diário

Estado: implementação e piloto técnico concluídos em 2026-10-04; execução estadual e promoção
do v2 ainda pendentes. O [contrato implementado e os resultados do piloto](score_v2.md) são a
referência para fórmulas, parâmetros, carga e limites operacionais.

## Escopo acordado

- Reutilizar somente os dados locais já existentes das fontes atuais.
- Não ampliar catálogo, período, download ou transformação Sentinel-2.
- Não baixar novas profundidades SoilGrids nem ampliar o período NASA POWER nesta etapa.
- Manter Mato Grosso, período 2023-09-01 a 2024-04-30, grade de 1 km e filtro de soja vigente.
- Calcular o balanço diariamente e publicar resultados semanais.
- Preservar o método `academic-index-v1`, seus arquivos, tabela e consumidores.
- Modelar a reserva na camada de 0–30 cm. Não extrapolar seus atributos para raízes mais profundas.
- Tratar calendário, parâmetros agronômicos e água inicial como hipóteses de cenário.
- Referências metodológicas são bibliografia da implementação, não novas fontes de dados ingeridas.

Hoje `gold-consumption-v2` é a versão do contrato de consumo; a fórmula continua
`academic-index-v1`. A nova fórmula terá identificação própria, separada do contrato e da
política `four-level-v1` de classificação.

## Decisões de produto confirmadas

| Decisão | Escolha confirmada | Alternativa considerada e implicação |
|---|---|---|
| Demanda da cultura | Kc por fase com calendário configurável. Representa variação durante o ciclo, mas exige datas, coeficientes e durações estimados. | Kc constante. Mais simples e útil como referência, mas perde a variação por fase. |
| Composição do indicador | Novo score combinado: componente hídrico, NDVI e NDMI; pesos 50/30/20 inicialmente provisórios. Permite continuidade da apresentação, mas os valores não são equivalentes aos do v1. | Publicar somente o componente hídrico novo ao lado do score atual. Facilita diagnosticar o modelo antes de escolher pesos. |
| Água inicial | Comparar 25%, 50% e 75% da capacidade disponível. Cenários ilustrativos, não medições nem intervalo de confiança. O de 50% é candidato a principal. | Somente 50%. Menor custo local e volume de saída, mas não revela sensibilidade à condição inicial. |

As três decisões foram apresentadas com suas implicações e confirmadas pelo usuário nesta sessão.
O usuário confirmou plantio de cenário em 15/10/2023 e fases de 20/30/60/30 dias.
Kc 0,40/1,15/0,50, p=0,50, conversão carbono/matéria orgânica=1,724 e escoamento=0
foram registrados como referências/hipóteses configuráveis, não calibração local.
Não inferir o plantio observado a partir da máscara anual de soja ou de uma data única estadual.
Se não houver calendário informado, usar explicitamente um calendário de cenário; nunca apresentá-lo
como calendário observado.

## Escolhas técnicas propostas

| Tema | Proposta | Implicação |
|---|---|---|
| Retenção | Estimar capacidade de campo e ponto de murcha com Saxton–Rawls a partir de textura e matéria orgânica estimada. Usar o Silver 0–30 cm como camada homogênea aproximada. | Sem reprocessar rasters; aplicar uma função não linear às médias é uma aproximação que deve constar na linhagem. |
| Carbono orgânico | Converter `soc` de g/kg para a unidade exigida pela função; tornar explícita a relação adotada entre carbono e matéria orgânica. | Carbono e matéria orgânica não são a mesma variável. O coeficiente e sua referência devem ser definidos, não inferidos silenciosamente. |
| Densidade | Usar `bulk_density` como informação de contexto/coerência; incluir correção hidráulica apenas se a equação estiver especificada e testada. | Evita afirmar que uma correção foi aplicada antes de implementá-la. |
| Profundidade | Reservatório fixo de 0,30 m nesta etapa. | O resultado representa esgotamento superficial sob demanda estimada da cultura; não é um modelo completo da zona radicular. |
| Irrigação e contribuição subterrânea | Cenário de sequeiro com irrigação e ascensão capilar iguais a zero. | Hipóteses, não identificação de todas as células como lavouras realmente não irrigadas. |
| Escoamento | Começar com escoamento zero como referência simplificada, registrando a hipótese. | Pode superestimar entrada de água. Um coeficiente de perda como cenário alternativo é possível, mas não representa uma estimativa calibrada. |
| Drenagem | Remover a água que excede a capacidade do reservatório depois do consumo diário. | Estimativa por excesso de armazenamento; sem descrição de condutividade ou intensidade horária da chuva. |
| Satélite | Reutilizar seleção, limites de NDVI/NDMI e idade máxima atuais. | Isola o efeito do novo componente hídrico. Sem anomalias históricas ou validação fenológica dos índices nesta entrega. |
| Persistência | Contar dias com Ks < 1; sequência continua entre semanas dentro do ciclo válido. | Indicador adicional; não acrescentar peso de persistência ao score nesta primeira comparação. |
| Falha de entrada | Invalidar a continuidade após uma lacuna meteorológica; recomeçar somente com uma condição inicial explicitamente registrada. | Não manter o estado anterior como se não tivesse havido consumo/chuva no dia ausente. |

Os coeficientes de Kc e as durações por fase requerem parâmetros de referência e identificação
da hipótese local. Base metodológica: [FAO, coeficientes da cultura](https://www.fao.org/4/x0490e/x0490e0b.htm).
A função de retenção necessita validação de aplicação e unidades:
[Saxton–Rawls, USDA](https://www.ars.usda.gov/research/publications/publication/?seqNo115=178719).
Não haverá validação agronômica independente nesta etapa, devido à restrição de dados.

## Contrato matemático adotado

Proposta adaptada de reservatório diário, com coeficiente único de cultura. Não alegar
implementação integral da FAO-56. A capacidade disponível é estimada como
`TAW = 1000 × (theta_fc - theta_wp) × 0,30`, em mm. O limite de depleção sem estresse é
`RAW = p × TAW`; `p` é um parâmetro explícito. A demanda potencial é `ETc_pot = Kc × ETo`.
Base: [FAO, disponibilidade e estresse hídrico](https://www.fao.org/4/x0490e/x0490e0e.htm).

A seguinte ordem de eventos é uma escolha de discretização do projeto: entrada de chuva,
cálculo de estresse, consumo e drenagem ao final do dia. Ela usa precipitação diária e
não identifica o horário real dos eventos. Com `A` representando água disponível acima do
ponto de murcha, a sequência proposta é:

```text
A0 = fração inicial × TAW
P_net = P × (1 - fração de escoamento)
A_temp = A_anterior + P_net
A_para_Ks = min(TAW, A_temp)
Ks = limitar(A_para_Ks / ((1 - p) × TAW), entre 0 e 1)
ETc_adj = min(A_temp, Ks × ETc_pot)
drenagem = max(0, A_temp - ETc_adj - TAW)
A_final = A_temp - ETc_adj - drenagem
chuva_aproveitada = P_net - drenagem
estresse_diário = 1 - Ks
```

`chuva_aproveitada` é uma estimativa dessa convenção, não uma medição de chuva efetiva.
Não descontar escoamento ou drenagem uma segunda vez. Validar conservação de massa e
`0 <= A_final <= TAW`, `0 <= chuva_aproveitada <= P`, `0 <= Ks <= 1`.
Exigir `TAW > 0`, `0 < p < 1`, textura coerente, unidades corretas e valores finitos.
Propriedades inválidas geram estado indisponível e motivo explícito, sem ajustes silenciosos.

Antes do ciclo de cenário não haverá score v2. A implementação inicializa no plantio e exige
cobertura do estudo a partir desse dia quando o ciclo estiver ativo; calendário começando antes
do estudo é recusado para evitar atribuir a condição inicial a outra data. Após o ciclo, indicar resultado não aplicável, distinto
de ausência de dados e de score zero. Não simular solo descoberto com Kc zero como se fosse
um modelo de evaporação fora do ciclo.

O componente hídrico semanal será a média de `1 - Ks` nos dias ativos esperados. Não dividir
automaticamente por sete: extremos do estudo e semanas de início/fim do ciclo podem ser parciais.
Se faltar estado válido em qualquer dia ativo esperado, não produzir o componente da semana.
Registrar dias ativos esperados, dias válidos, máximo de estresse, dias sob estresse, maior
sequência dentro da semana e sequência em andamento ao fim da semana. Uma sequência em andamento
pode exceder sete dias; a contagem de dias da semana não pode.

No score combinado escolhido, substituir somente o componente de déficit pelo novo componente
hídrico, mantendo as transformações de NDVI/NDMI para a primeira comparação. Com todos disponíveis:
`score_v2 = 0,50 × estresse_hídrico + 0,30 × estresse_NDVI + 0,20 × estresse_NDMI`.
Ausência de satélite permite média dos componentes disponíveis, com pesos e status explícitos;
ausência do componente hídrico impede o score v2. A comparação principal deve separar scores
completos e parciais, idade do satélite e quantidade de dias ativos.

As quatro faixas podem ser reutilizadas como interpretação provisória, mantendo versão e
orientações acadêmicas. A classe de v2 não é agronomicamente equivalente à mesma classe de v1.

## Dados e arquitetura

Datasets implementados (detalhes e schemas em [score_v2.md](score_v2.md)):

| Dataset | Grão/chave | Conteúdo |
|---|---|---|
| Gold de propriedades hidráulicas | `analysis_id + grid_id` | Umidades estimadas, TAW superficial, método e assinatura. Calculado uma vez por bloco e reutilizado entre semanas/cenários. |
| Gold semanal v2 | `analysis_id + grid_id + week_start + scenario_id` | ETc potencial/ajustada e chuva aproveitada acumuladas; água inicial/final da semana; componente hídrico médio/máximo; persistência; índices e idade do satélite; score opcional, classe, status e motivo. |
| Checkpoint por bloco espacial e semana | bloco, semana e cenário | Estado final diário por célula, sequência em andamento e validade; assinatura de entradas, parâmetros e checkpoint precedente. |

A dimensão espacial continua em EPSG:5880 e com resolução de 1 km, sem reprojeção ou
reamostragem adicional. Todas as lâminas de água usam mm, umidades volumétricas usam m³/m³,
frações e índices são adimensionais, datas e contagens usam contratos explícitos.
As células compartilham a meteorologia NASA POWER conforme a associação atual; a grade de
saída de 1 km não implica meteorologia observada nessa resolução.

Processar blocos de células em ordem temporal, com arrays de estado de tamanho limitado.
Não persistir a tabela diária estadual inteira por padrão. Agregar durante o processamento,
persistir o semanal e os estados necessários à retomada. Guardar diagnóstico diário somente
para o piloto, quando solicitado. Três cenários aumentam a simulação/saída, mas compartilham
leituras de meteorologia, satélite e propriedades hidráulicas.

Particionar por chaves estáveis de estudo, cenário, semana e bloco espacial. Registrar versões,
parâmetros, pressupostos, timestamp e checksums. Escritas atômicas; uma alteração de estado/entrada
invalida checkpoints subsequentes do mesmo bloco/cenário. A retomada deve conferir a cadeia
desde a inicialização, não reutilizar semanas posteriores com estado incompatível.

O consumidor selecionará um cenário principal. Nunca somar áreas ou calcular médias juntando
linhas de cenários diferentes. Área de soja continua a ponderar agregações dentro de um cenário,
com cobertura e status disponíveis junto aos resultados. Uma migração aditiva criará o consumo
v2 sem substituir a tabela/view atual nem copiar geometria estática para cada semana.

## Entregas e critérios de avanço

1. **Fechar o contrato e parâmetros.** Registrar escolhas do usuário, calendário, coeficientes,
   conversão de carbono, p, água inicial, perdas e fonte de cada hipótese. Inspecionar somente
   schemas, metadados e disponibilidade dos arquivos locais necessários. Não iniciar cargas.
2. **Implementar regras puras e configuração.** Funções de retenção, balanço diário, agregação e
   score; contratos tipados e configuração Pydantic. Separar regras de CLI, banco e caminhos.
   Reusar NumPy/Arrow existentes; nenhuma dependência nova é prevista.
3. **Validar com fixtures sintéticas.** Cobrir textura inválida, zero/nulos, unidades, dias secos,
   chuva intensa, capacidade excedida, conservação de massa, ciclo inativo, semanas parciais,
   lacunas de estado, sequência atravessando semana, satélite ausente/antigo e classes nos limites.
4. **Implementar persistência e retomada.** Testar processamento por blocos, escrita interrompida,
   reuso íntegro, recálculo em cadeia e igualdade entre execução contínua e retomada. Confirmar
   que outputs v1 não são modificados. Medir memória e tempo antes de decidir concorrência.
5. **Executar piloto local pequeno.** Selecionar células por regras reproduzíveis de solo e
   cobertura, sem nova leitura de COGs. Comparar v1/v2 por semana, componentes, status e cenários.
   Relatório de sensibilidade e coerência; não rotular essa comparação como validação de campo.
6. **Integrar consumo e ampliar execução.** Migração aditiva, carga incremental/idempotente,
   controles do cenário principal e qualidade. Testar em banco temporário antes do banco do
   projeto. Atualizar Airflow para um modo que reutilize Silver, sem ingestão nem Sentinel-2.
   Somente executar todo o estado depois de revisar o piloto e os custos medidos.

Testes padrão sem rede. Antes de entregar código: pytest, Ruff, formatação e mypy para o
escopo afetado, documentação atualizada e nenhuma credencial/dado baixado versionado.
O dashboard permanece uma etapa posterior; a tecnologia visual não interfere nessas regras.

## Estado da entrega

- Regras, schemas, CLI, checkpoints, carga aditiva e modo Airflow implementados e testados.
- Piloto técnico determinístico nas primeiras 32 células da Gold existente, sem seleção
  representativa agronômica; inclui casos sem solo e sem satélite. Testes sintéticos cobrem
  composição completa e sensibilidade em seca. Limitação e resultados registrados no contrato.
- Migração/carga verificadas em PostgreSQL/PostGIS temporário; banco do projeto intacto.
- Pendente: revisão do piloto, ampliação da amostra com cobertura de satélite e definição de
  promoção do v2 e execução estadual. Sem commit ou push.

Não alterar coeficientes automaticamente para fazer o v2 concordar com o v1. Sem observações
independentes, relatar consistência, sensibilidade e limitações; não alegar acurácia agronômica.

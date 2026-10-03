# Arquitetura de dados estadual

## Decisão

A área padrão do MVP passa do município de Sorriso para o estado de Mato Grosso (`state_code=51`).
O período inicial permanece a safra 2023/2024.

Essa mudança não significa processar todo pixel de alta resolução para todo dia. A arquitetura usa
agregação espacial, separação entre atributos estáticos e temporais e processamento seletivo.

## Resoluções e temporalidades

| Produto | Resolução/periodicidade | Papel |
|---|---|---|
| Grade estadual | 1 km | screening e chave espacial comum |
| Grade detalhada | 250 m | futura análise apenas em áreas críticas |
| MapBiomas | 30 m, anual | fração de soja por célula e filtro precoce |
| Sentinel-2 | 10/20 m, por cena | índices agregados; raster intermediário transitório |
| SoilGrids | 250 m, estático | atributos agregados uma única vez |
| NASA POWER | resolução nativa, diário | clima regional por célula meteorológica |
| Gold | 7 dias inicialmente | score e features necessárias ao modelo |

## Modelo lógico pretendido

### `dim_spatial_grid`

- `grid_id`
- `geometry`
- `centroid_latitude`
- `centroid_longitude`
- `area_km2`

### `crop_mask`

- `grid_id`
- `year`
- `soy_fraction`

### `soil_features`

- `grid_id`
- `clay_pct`
- `sand_pct`
- `soc`
- `bulk_density`

### `weather_daily`

- `weather_cell_id`
- `date`
- variáveis meteorológicas e ETo

### `satellite_observation`

- `grid_id`
- `date`
- `tile_id`
- NDVI/NDMI e percentis
- percentual de pixels válidos e nuvens

### Gold

A Gold une somente os atributos necessários na janela configurada. Geometria, solo e máscara não
devem ser repetidos diariamente em uma tabela estadual monolítica. A primeira tabela é
`water_stress_weekly`, particionada por `week_start` e com chave `grid_id + week_start`.

## Persistência relacional PostgreSQL/PostGIS

### Decisão

O PostgreSQL/PostGIS é uma camada de consulta e serving opcional para o ambiente local e para uma
futura implantação gerenciada. Ele não substitui os arquivos Bronze nem o armazenamento de objetos:

| Camada | Persistência principal | Conteúdo no PostgreSQL |
|---|---|---|
| Bronze | arquivos originais ou object storage | manifestos e linhagem em `bronze.artifact_manifest` |
| Silver | Parquet/GeoParquet/GeoTIFF | tabelas temáticas e geometrias em `silver` |
| Gold | Parquet particionado e tabela relacional | `gold.water_stress_weekly` |
| Controle | — | migrations, execuções e cargas em `control` |

Essa separação preserva os bytes originais, mantém o custo de armazenamento sob controle e permite
trocar o armazenamento local por S3/ADLS ou um PostgreSQL gerenciado sem alterar as regras de
negócio.

### Contrato de carga

- `migrations/001_initial.sql` cria os schemas, PostGIS, tabelas, chaves, verificações e índices;
- `dim_spatial_grid` é a dimensão espacial e a tabela pai das referências `grid_id`;
- `weather_daily` cria a geometria de ponto em `EPSG:4326`;
- `dim_spatial_grid` mantém polígonos em `EPSG:5880`;
- cargas usam staging temporário, `COPY`, transação e `ON CONFLICT`;
- cada dataset registra execução em `control.load_run` e `control.dataset_load`;
- reexecutar a carga é seguro e não duplica chaves;
- uma falha de carga não modifica o arquivo Parquet de origem;
- o pipeline não copia TIFFs, COGs ou GeoJSONs brutos para o banco.

### Operação local

`run_database` recebe host, porta, database e usuário por `WATER_STRESS_DATABASE__...`. O banco
fica desabilitado por padrão (`enabled=false`) para que ingestões e transformações locais não
dependam de um servidor ativo. O pgAdmin é somente cliente de administração e visualização; a
criação e a carga das tabelas são responsabilidades do pipeline.

### Evolução

O schema relacional é uma projeção consultável dos artefatos versionados. Migrações futuras devem
ser aditivas ou documentar explicitamente uma migração de contrato. O uso em nuvem deve substituir
somente a conexão e o armazenamento, preservando chaves, partições, manifestos e regras de carga.

## Decisões implementadas nesta etapa

### AOI genérica

`StudySettings` usa `area_type`, `area_code` e `area_name`. O código valida dois dígitos para UF e
sete para município. Caminhos usam `state_code=51`, o que evita nomes municipais embutidos no
código e prepara particionamento em object storage.

### Grade de 1 km

A transformação `spatial-grid` cria GeoParquet com identificador determinístico. Consultas usam
EPSG:4326; área, extensão e grade usam SIRGAS 2000 / Brazil Polyconic (`EPSG:5880`). Isso evita
cálculos métricos sobre graus.

### NASA POWER regional

O bounding box estadual excede o limite de 10° por eixo da API e é dividido em quatro regiões.
Cada região e parâmetro gera uma requisição e um artefato Bronze, totalizando 28. A separação
respeita as limitações da API regional e permite retry/idempotência por região e parâmetro. A
Silver regional deverá unir latitude, longitude e data, recortando células fora da geometria da UF.

A tabela Silver `weather_daily` usa a grade MERRA-2 de 0,5° latitude × 0,625° longitude como chave
espacial, com `weather_cell_id` determinístico e uma linha por data. Como a radiação solar possui
grade SYN1DEG de 1° × 1°, ela é harmonizada pelo centro vizinho mais próximo, limitado à distância
angular máxima entre essas grades. A escolha e a distância máxima observada são persistidas nos
metadados. A ETo diária é calculada pela FAO-56 Penman–Monteith usando elevação, temperaturas,
umidade relativa média, vento e radiação; entradas incompletas produzem ETo nula.
No balanço diário, o fluxo de calor do solo é assumido zero e `Rs/Rso` é limitado ao intervalo
FAO-56 de 0,3 a 1,0; as premissas são registradas junto ao dataset.

### SoilGrids em chunks

A extensão projetada é dividida em blocos de 250 km com `chunk_id` determinístico. Cada propriedade
e profundidade é independente, reduzindo o impacto de falhas e preparando paralelização futura.

A tabela Silver `soil_features` atribui centros dos pixels nativos de 250 m às células da grade de
1 km, sem interpolação, e calcula a média espacial de cada propriedade. As camadas 0–5, 5–15 e
15–30 cm são combinadas por média ponderada pela espessura para representar 0–30 cm. Argila e areia
são convertidas de g/kg para %, carbono orgânico de dg/kg para g/kg e densidade aparente de cg/cm³
para g/cm³. Células sem cobertura completa nas três profundidades permanecem nulas.
O WCS pode ajustar em menos de 1% a resolução de chunks parciais de borda; essa variação é validada
e registrada, sem reamostragem, e resoluções fora dessa tolerância interrompem a transformação.
Como o WCS não declara `nodata` nos GeoTIFFs observados, valores brutos menores ou iguais a zero
são considerados preenchimento ausente para estas quatro propriedades, todas de domínio físico
estritamente positivo.

### Sentinel-2 orientado a catálogo/tile

O padrão estadual persiste apenas o catálogo STAC. O download Bronze dos COGs foi desativado por
padrão porque materializar cenas estaduais completas contradiz a estratégia de custo. A próxima
Silver deverá:

1. cruzar tiles com a máscara de soja;
2. ignorar tiles sem soja;
3. ler B04, B08, B11 e SCL;
4. aplicar nuvens e recorte;
5. calcular NDVI/NDMI;
6. agregar diretamente para `grid_id`;
7. descartar intermediários de alta resolução.

A tabela `satellite_observation` implementa esse fluxo incrementalmente por item STAC. A seleção
inicial mantém uma cena por combinação de ano, mês e tile, prioriza tiles com maior quantidade
estimada de células de soja cobertas e limita a 15 tiles por mês por padrão; `--tiles-per-month`
ajusta esse limite e `--coverage all` permite processar explicitamente todo o catálogo. A seleção usa células com `soy_fraction > 0`; a
máscara final usa diretamente a classe 39 MapBiomas reamostrada por vizinho mais próximo para 10 m.
Partições completas já existentes contam para a cota mensal, evitando reprocessar o mês quando o
limite foi atingido.
B11 usa bilinear de 20 m para 10 m, enquanto SCL usa vizinho mais próximo. Classes SCL 4–7 são
válidas e 8–10 são nuvens. NDVI/NDMI são agregados por `grid_id` com média e percentis aproximados
por histograma de 400 classes. Cada item constitui uma partição idempotente e nenhum raster
intermediário é materializado.
Reflectâncias não positivas depois da escala e offset L2A são excluídas dos dois índices.
O índice espacial de células candidatas é pré-calculado uma vez por `tile_id` em cada execução,
evitando repetir a transformação de toda a grade para cada cena. A leitura das cenas pode usar até
dois workers locais, enquanto os artefatos continuam sendo gravados de forma serializada e atômica.

### MapBiomas

O raster original continua na Bronze. A tabela Silver `crop_mask` agrega a classe de soja por
`grid_id` e ano, usando a fração de centros de pixels válidos dentro da geometria estadual. Valores
categóricos não são interpolados. Esse método é documentado como
`source_pixel_center_count`, produz `soy_fraction` adimensional entre 0 e 1 e fornece o filtro
precoce necessário ao processamento Sentinel-2.

## Pendências deliberadas

- grade adaptativa de 250 m em hotspots;
- score semanal e calibração dos pesos;
- composição temporal definitiva do Sentinel-2.

Essas pendências não são marcadas como concluídas porque exigem contratos de qualidade e testes
geoespaciais próprios. A fundação entregue define as chaves, partições, CRS e limites de
materialização necessários para implementá-las sem retrabalho arquitetural.

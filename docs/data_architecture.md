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
devem ser repetidos diariamente em uma tabela estadual monolítica.

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

### MapBiomas

O raster original continua na Bronze. A tabela Silver `crop_mask` agrega a classe de soja por
`grid_id` e ano, usando a fração de centros de pixels válidos dentro da geometria estadual. Valores
categóricos não são interpolados. Esse método é documentado como
`source_pixel_center_count`, produz `soy_fraction` adimensional entre 0 e 1 e fornece o filtro
precoce necessário ao processamento Sentinel-2.

## Pendências deliberadas

- processamento transitório Sentinel por tile;
- grade adaptativa de 250 m em hotspots;
- tabelas Gold e score semanal.

Essas pendências não são marcadas como concluídas porque exigem contratos de qualidade e testes
geoespaciais próprios. A fundação entregue define as chaves, partições, CRS e limites de
materialização necessários para implementá-las sem retrabalho arquitetural.

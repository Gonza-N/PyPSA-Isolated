# Caso Magallanes: cómo reproducir la base

Este fork usa PyPSA-Isolated (V. Sepúlveda, versión `577951d`) para el caso de los cuatro sistemas medianos de Magallanes: Punta Arenas, Puerto Natales, Porvenir y Puerto Williams. La base consolidada corresponde a la iteración 12, del 23 de noviembre de 2026.

## Qué cambia respecto del repositorio original

- **Datos del caso** (`input_magallanes/`): demanda, parque existente, costos, límites, almacenamiento, hidrógeno y líneas, cada valor con su fuente en la columna `source`. Se agregaron dos nodos eólicos, la ZAE de Porvenir y Laguna Blanca, ambos dentro de las Zonas de Aptitud Energética del PEER de Magallanes.
- **Cuaderno** (`case_analysis_Magallanes.ipynb`): trae la escala de 13 escenarios, los años 2025, 2030, 2040, 2045 y 2050, la regla de topes de emisiones `bau_XX_2050`, un límite de 21.600 s por caso para Gurobi (los casos de la base terminan antes de 10.800 s) y la simulación activada (`RUN_SIMULATIONS = True`).
- **Código** (`config.py`): un solo cambio. Las líneas ampliables parten cada año con la capacidad que dejó el año anterior, igual que los generadores y el almacenamiento.

## Escenarios

- **S1 a S3:** BAU (parque existente congelado), RE0 (renovables sin tope de emisiones) y RE80 (80 % menos emisiones que el BAU en 2050), aislados.
- **S4 y S5:** RE95, aislado y con ambas conexiones.
- **S6 a S9:** NZE, cero emisiones en 2050 con renovables y baterías, en las cuatro topologías.
- **S10 a S13:** H2, NZE más la cadena de hidrógeno, en las cuatro topologías.

Las topologías son aislada (`interlinks_disconnected.csv`), Punta Arenas con Porvenir (`interlinks_porvenir.csv`), Punta Arenas con Puerto Natales (`interlinks_natales.csv`) y ambas (`interlinks_natales_porvenir.csv`). Puerto Williams queda siempre aislado. La línea de la ZAE de Porvenir y el tramo Laguna Blanca–Puerto Natales están en todas, porque unen un parque con un solo sistema.

## Requisitos

- El entorno `pypsa-isolated` (PyPSA 1.2.4, linopy 0.9.0 y atlite).
- Gurobi, con licencia. Todos los escenarios son problemas enteros mixtos, porque los tramos de los corredores se construyen por módulos de 33 MW.
- Los recortes de clima de ERA5 en `data/`, que no se versionan porque pesan unos 300 MB cada uno:
  - `data/magallanes_2025.nc` es el clima de la base. Si no está, se arma con atlite poniendo `prepare_cutout = True` en la celda 5. Para eso hace falta una cuenta del Climate Data Store de Copernicus y su archivo `~/.cdsapirc`.
  - `data/magallanes_2016.nc` a `magallanes_2024.nc` los usa solo la robustez (celda 37). Se arman de la misma forma, cambiando por un momento `critical_weather_year` a cada año y volviéndolo a 2025 antes de correr la base. Si no se tienen, la celda 37 lo avisa y el cuaderno sigue.

## Cómo correr

1. Abrir `case_analysis_Magallanes.ipynb` con el kernel del entorno.
2. Ejecutar todas las celdas (Run All).

La corrida resuelve los 13 escenarios en sus cinco años (65 casos), hace las tablas y figuras y al final la robustez con los años 2016 a 2025. Toma entre 10 y 14 horas. Todo queda en `output/output_magallanes/`: `manifest.csv`, `simulations/<escenario>/<año>/`, `tables/`, `figures/` y `robustness/`.

- Para correr solo algunos escenarios, se edita `RUN_SCENARIO_CODES` en la celda 5. Si un escenario tiene tope de emisiones, el cuaderno resuelve antes el BAU, porque el tope se calcula con sus emisiones. `ACTIVE_SCENARIO` (celda 3) debe ser uno de los escenarios corridos.
- Para retomar una corrida interrumpida, se pone `OVERWRITE_EXISTING_EXPORTS = False` (celda 3) y los escenarios completos se saltan. Después de cambiar un insumo debe quedar en `True`.
- Si un escenario falla (por ejemplo, por el límite de tiempo), los demás siguen y al final se avisa cuáles faltan. Se resuelven solo esos con `OVERWRITE_EXISTING_EXPORTS = False`.
- Para volver a analizar resultados ya calculados sin optimizar de nuevo, se pone `RUN_SIMULATIONS = False`.

## Cómo leer los resultados

- `metadata.json` de cada caso trae el objetivo del optimizador, que es la cuota anual de lo que se construye ese año más los costos variables. El costo completo del sistema suma además la cuota de lo que ya existía al empezar el año y el costo fijo de las térmicas existentes, y no incluye la penalización de la energía no suministrada. Lo calcula la última celda y queda en `tables/average_system_cost_by_scenario.csv`.
- La brecha de optimalidad pedida a Gurobi es de 2 % entre 2025 y 2045 y de 0,1 % en 2050 (`mip_gap_by_year`, celda 5), el año en que se comparan los escenarios. Con 2 % en 2050, un caso conectado salía más caro que el aislado. La brecha, la cota y el tiempo de cada caso quedan en `metadata.json`.
- Las series horarias están en UTC, tres horas adelantadas respecto de Magallanes. 2040 es bisiesto (8.784 horas) y su demanda queda 0,22 % sobre la de entrada.

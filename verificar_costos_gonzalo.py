"""Verificación breve de costos y almacenamiento de Magallanes, sin solver."""
# Cambio: consolida las comprobaciones permanentes en redes sintéticas sin optimizar.
# Ejecutar desde la raíz: python -B verificar_costos_gonzalo.py
from pathlib import Path
import sys

import numpy as np
import pandas as pd

import config

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


ROOT = Path(__file__).resolve().parent
INPUT = ROOT / "input_magallanes"
YEARS = [2025, 2030, 2040, 2050]
ETA_BESS = np.sqrt(0.92)


def build_network(year, *, scenario="RH_interconnected", previous=None):
    """Construye dos horas sintéticas; no llama a optimize ni a un solver."""
    nodes = config.load_nodes_from_csv(str(INPUT / "nodes.csv"))
    snapshots = pd.date_range("2025-01-01", periods=2, freq="h")
    zones = nodes.zone.tolist()
    load = pd.DataFrame(0.0, index=snapshots, columns=zones)
    profiles = {kind: load + 0.5 for kind in ("wind", "solar")}
    bau = scenario.startswith("BAU")
    connected = scenario.endswith("_interconnected")
    return config.build_case_network(
        snapshots=snapshots, nodes_df=nodes, load=load,
        hydro_assets_df=pd.DataFrame(), year=year, vre_profiles=profiles,
        enable_hydrogen=scenario.startswith("RH"), enable_ptx=False,
        general_settings=config.load_general_settings_from_csv(str(INPUT / "general.csv")),
        previous_year_network=previous,
        csv_costs=str(INPUT / "costs.csv"),
        csv_capacity=str(INPUT / ("generators_capacity_BAU_base.csv" if bau else "generators_capacity.csv")),
        csv_storage=str(INPUT / ("storage_capacity_BAU_base.csv" if bau else "storage_capacity.csv")),
        csv_hydrogen=str(INPUT / "hydrogen_assets.csv"),
        csv_interlinks=str(INPUT / ("interlinks_interconnected.csv" if connected else "interlinks_disconnected.csv")),
    )


def set_synthetic_optima(net, *, bess_mw=0.0, fuel_cell_mw=0.0):
    """Asigna capacidades sólo para probar cohortes; no son resultados del modelo."""
    for component, nominal in (("generators", "p_nom"), ("storage_units", "p_nom"),
                               ("links", "p_nom"), ("stores", "e_nom")):
        table = getattr(net, component)
        table[nominal + "_opt"] = table[nominal]
    net.storage_units.loc[:, "p_nom_opt"] = bess_mw
    net.links.loc[net.links.carrier.eq("h2_fuel_cell"), "p_nom_opt"] = fuel_cell_mw
    config.finalize_capacity_cohorts(net)


def main():
    if Path.cwd().resolve() != ROOT:
        raise SystemExit(f"Ejecutar desde {ROOT}")

    raw = config.load_technology_costs_from_csv(str(INPUT / "costs.csv"))
    assert len(raw) == 60 and not raw.duplicated(["cost_key", "year"]).any()
    assert set(raw.year) == set(YEARS)
    assert raw.discount_rate.dropna().eq(0.07).all()

    generators = pd.read_csv(INPUT / "generators_capacity.csv")
    storage = pd.read_csv(INPUT / "storage_capacity.csv")
    hydrogen = pd.read_csv(INPUT / "hydrogen_assets.csv")
    assert set(generators.cost_key) <= set(raw.cost_key)
    assert set(storage.cost_key) == {"bess_4h", "bess_8h"}
    assert set(hydrogen.cost_key) == {"electrolyzer", "h2_fuel_cell", "h2_tank"}

    for year in YEARS:
        costs = config._project_costs_for_year(raw, year).set_index("cost_key")
        assert len(costs) == 15
        for key, life in {"wind_new": 30, "solar_utility_new": 40,
                          "solar_residential_new": 40, "bess_4h": 15,
                          "bess_8h": 15}.items():
            row = costs.loc[key]
            assert row.lifetime_years == life and row.fom_fraction == 0.015
            crf = 0.07 / (1 - (1.07) ** (-life))
            assert np.isclose(row.capital_cost, row.capex * (crf + 0.015))
        assert costs.loc["bess_4h", "max_hours"] == 4
        assert costs.loc["bess_8h", "max_hours"] == 8
        assert costs.loc["bess_8h", "capex"] > costs.loc["bess_4h", "capex"]
        assert np.isclose(costs.loc["bess_4h", "efficiency_store"], ETA_BESS)
        assert np.isclose(costs.loc["bess_8h", "efficiency_dispatch"], ETA_BESS)

    net = build_network(2030)
    assert len(net.storage_units) == 8
    assert net.storage_units.cyclic_state_of_charge.all()
    assert np.isclose(net.storage_units.efficiency_store * net.storage_units.efficiency_dispatch, 0.92).all()
    assert net.storage_units.standing_loss.eq(0.001).all()
    assert len(net.stores) == 4 and net.stores.e_cyclic.all()
    assert net.stores.standing_loss.eq(0).all()

    # Cada generador térmico recibe una sola fila agregada CVC+CVNC; el gas térmico
    # desactivado no se suma otra vez ni se vuelve a dividir por eficiencia.
    costs_2030 = config._project_costs_for_year(raw, 2030).set_index("cost_key")
    asset_cost_key = generators.set_index("asset_id").cost_key
    configured = net.generators.index.intersection(asset_cost_key.index)
    for asset in configured:
        expected = costs_2030.loc[asset_cost_key[asset], "marginal_cost"]
        assert np.isclose(net.generators.at[asset, "marginal_cost"], expected)
    assert not net.generators.carrier.eq("natural_gas").any()

    tx = net.links[net.links.index.str.startswith("tx_")]
    assert np.isclose((tx.p_nom * tx.capital_cost).sum(), 22_300_000)
    porvenir = tx[tx.index.str.contains("porvenir")]
    assert len(porvenir) == 2 and porvenir.efficiency.eq(0.96).all()
    assert sorted(porvenir.capital_cost.tolist()) == [0, 50_000]
    assert np.isclose((porvenir.p_nom * porvenir.capital_cost).sum(), 1_500_000)

    bau = build_network(2025, scenario="BAU_disconnected")
    assert bau.storage_units.empty and bau.stores.empty
    bau_tx = bau.links[bau.links.index.str.startswith("tx_")]
    assert np.isclose((bau_tx.p_nom * bau_tx.capital_cost).sum(), 16_000_000)

    # Una ampliación identificada en 2030 vence por vida técnica antes de 2050.
    set_synthetic_optima(net, bess_mw=1.0, fuel_cell_mw=2.0)
    future = build_network(2050, previous=net)
    assert future.storage_units.p_nom.eq(0).all()
    assert future.links.loc[future.links.carrier.eq("h2_fuel_cell"), "p_nom"].eq(0).all()

    print("OK: 60 costos, cuatro años, claves de activos, unidades y anualización al 7%.")
    print("OK: BESS 4/8 h, RTE 92%, ciclos, pérdidas BESS/tanques y vencimiento de cohortes.")
    print("OK: transmisión se cobra una vez; PA-Porvenir=1.500.000 USD/año.")
    print("OK: costo térmico agregado se asigna una vez; no se suma gas térmico ni CVNC adicional.")
    print("ALCANCE: redes de dos horas sin solver; no certifica normalización monetaria ni edades de la flota inicial.")


if __name__ == "__main__":
    main()

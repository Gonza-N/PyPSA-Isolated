"""
Build a plant-level PyPSA generation-capacity table from the MBIE EDGS 2024
assumptions workbook.

Design principles
-----------------
1. Keep one EDGS plant/project row as one model asset (no technology aggregation).
2. Keep spatial aggregation outside the source data using an explicit region map.
3. Keep non-source modelling assumptions explicit in this script and documented in the Supplementary Method.
4. Reconstruct thermal short-run marginal costs from EDGS commodity prices,
   plant heat rates, delivery costs, variable O&M, and the EDGS effective carbon price.
5. Preserve all source fields needed to audit commissioning and cost assumptions.
6. Write model capacities and plant-specific costs as separate, cost-key-linked tables.
7. Derive the no-new-thermal variant from the same source table, changing only enabled flags.

The script intentionally does NOT model hydro reservoir operation. HydRR/HydSC
plants remain plant-level capacity records here; inflows, reservoirs and cascades
belong in the separate hydro preprocessing/model layer.

Example
-------
python build_capacity_EDGS.py \
    --input-xlsx "data_NZ_generation/electricity-demand-generation-scenarios-2024-assumptions.xlsx" \
    --scenario Reference \
    --topology 14 \
    --nodes input_new_zealand/nodes.csv

For the previous 11-region REMix aggregation:
python build_capacity_EDGS.py --scenario Reference --topology 11

For all 16 administrative regions:
python build_capacity_EDGS.py --scenario Reference --topology 16

For model-ready filenames (also creates costs_generation.csv and
generators_capacity_no_new_thermal.csv beside it):
python build_capacity_EDGS.py --output input_new_zealand/generators_capacity.csv
"""

from __future__ import annotations

import argparse
import math
import unicodedata
from pathlib import Path
from typing import Iterable

import numpy as np
import pandas as pd


SCRIPT_VERSION = "2026-09-02-central-costs-scenarios-availability"
GENERATION_SHEET = "Generation Stack"
COMMODITY_SHEET = "Commodity prices"
DEFAULT_SCENARIO = "Reference"
DEFAULT_PLANNING_YEARS = [2025, 2030, 2040, 2050]
DEFAULT_BASE_YEAR = 2025
DEFAULT_CARBON_VARIABLE = "Effective carbon price"
DEFAULT_GEOTHERMAL_AVAILABILITY_FACTOR = 0.85
CSV_FLOAT_FORMAT = "%.6f"
BASE_DIR = Path(__file__).resolve().parent

# Technologies disabled in the automatically generated no-new-thermal variant.
# Existing plants remain enabled; only candidate/committed additions are disabled.
NEW_THERMAL_TECHNOLOGIES = {
    "bio_reciprocating",
    "ccgt",
    "coal",
    "diesel_peaker",
    "gas_cogeneration",
    "gas_peaker",
    "ocgt",
    "other_cogeneration",
}

# This is the same centralized schema used by the wider PyPSA-Isolated costs.csv.
# The EDGS builder writes only the generation rows, ready to concatenate with the
# rows for storage, hydrogen, heat and PtX technologies.
COSTS_OUTPUT_COLUMNS = [
    "cost_key", "technology", "year", "base_year", "capex", "capex_unit",
    "nominal_basis", "discount_rate", "lifetime_years", "fom_fraction",
    "capital_cost", "capital_cost_unit", "marginal_cost", "marginal_cost_unit",
    "efficiency", "max_hours", "efficiency_store", "efficiency_dispatch",
    "standing_loss", "co2_emissions", "annual_change", "capex_annual_change",
    "capital_cost_annual_change", "marginal_cost_annual_change",
    "co2_emissions_annual_change", "fixed_operating_cost_nzd_per_kw_year",
    "fuel_delivery_cost_nzd_per_gj", "heat_rate_gj_per_gwh",
    "connection_cost_nzd_m", "total_capital_costs_nzd_m", "source",
    "source_url", "fx_rate_usd_per_eur", "fx_source_url", "notes",
]

# Cost and efficiency fields intentionally do not appear here. They are resolved
# from costs.csv through cost_key. Plant, commissioning and spatial metadata stay
# in the capacity file so no EDGS asset identity is lost.
CAPACITY_OUTPUT_COLUMNS = [
    "asset_id", "generator", "zone", "technology", "cost_key",
    "installed_capacity", "p_min_mw", "availability_factor",
    "available_from_year", "earliest_commissioning_year",
    "fixed_commissioning_year", "commissioning_type",
    "commissioning_year_source", "decommissioning_year", "enabled", "status",
    "plant", "plant_slug", "tech", "tech_name", "plant_type", "substation",
    "region", "edgs_scenario", "edgs_earliest_commissioning_year",
    "edgs_fixed_commissioning_year", "topology", "requires_hydro_asset",
    "requires_vre_profile", "commissioning_policy", "builder_version",
]

# Explicit non-EDGS modelling assumptions. These are deliberately kept in this
# preprocessing script (and documented in the Supplementary Method) so the
# generation data package requires only one auxiliary CSV: the spatial
# EDGS-region-to-model-zone mapping.
TECHNOLOGY_ASSUMPTIONS = {
    "GasPkr": dict(technology="gas_peaker", fuel="natural_gas", commodity_price_variable="Natural gas wholesale price estimate", co2_emissions_t_per_mwh_th=0.202, availability_factor=1.00, wacc_real=0.07, lifetime_years=30, notes="Fuel/carbon costs use EDGS commodity prices; WACC/lifetime are study assumptions."),
    "OCGT": dict(technology="ocgt", fuel="natural_gas", commodity_price_variable="Natural gas wholesale price estimate", co2_emissions_t_per_mwh_th=0.202, availability_factor=1.00, wacc_real=0.07, lifetime_years=30, notes="Fuel/carbon costs use EDGS commodity prices; WACC/lifetime are study assumptions."),
    "CCGT": dict(technology="ccgt", fuel="natural_gas", commodity_price_variable="Natural gas wholesale price estimate", co2_emissions_t_per_mwh_th=0.202, availability_factor=1.00, wacc_real=0.07, lifetime_years=30, notes="Fuel/carbon costs use EDGS commodity prices; WACC/lifetime are study assumptions."),
    "GasCog": dict(technology="gas_cogeneration", fuel="natural_gas", commodity_price_variable="Natural gas wholesale price estimate", co2_emissions_t_per_mwh_th=0.202, availability_factor=1.00, wacc_real=0.07, lifetime_years=30, notes="Fuel/carbon costs use EDGS commodity prices; WACC/lifetime are study assumptions."),
    "Coal": dict(technology="coal", fuel="coal", commodity_price_variable="Coal wholesale price estimate", co2_emissions_t_per_mwh_th=0.341, availability_factor=1.00, wacc_real=0.07, lifetime_years=30, notes="Fuel/carbon costs use EDGS commodity prices; WACC/lifetime are study assumptions."),
    "DslPkr": dict(technology="diesel_peaker", fuel="diesel", commodity_price_variable="Diesel wholesale price estimate", co2_emissions_t_per_mwh_th=0.267, availability_factor=1.00, wacc_real=0.07, lifetime_years=30, notes="Fuel/carbon costs use EDGS commodity prices; WACC/lifetime are study assumptions."),
    "OthCog": dict(technology="other_cogeneration", fuel="", commodity_price_variable="", co2_emissions_t_per_mwh_th=0.202, availability_factor=1.00, wacc_real=0.07, lifetime_years=25, notes="Fuel type is not uniquely identified by EDGS Tech; review at plant level if material."),
    "BioRecip": dict(technology="bio_reciprocating", fuel="", commodity_price_variable="", co2_emissions_t_per_mwh_th=0.000, availability_factor=1.00, wacc_real=0.07, lifetime_years=25, notes="No wholesale fuel-price series assigned by default; review if material."),
    "Geo": dict(technology="geothermal", fuel="", commodity_price_variable="", co2_emissions_t_per_mwh_th=0.000, availability_factor=DEFAULT_GEOTHERMAL_AVAILABILITY_FACTOR, wacc_real=0.07, lifetime_years=30, notes="Geothermal availability is a configurable study assumption representing planned/forced outages; it is not an EDGS field."),
    "HydPK": dict(technology="hydro_peaking", fuel="", commodity_price_variable="", co2_emissions_t_per_mwh_th=0.000, availability_factor=1.00, wacc_real=0.07, lifetime_years=80, notes="Water availability is handled by the hydro module, not this static factor."),
    "HydRR": dict(technology="hydro_run_of_river", fuel="", commodity_price_variable="", co2_emissions_t_per_mwh_th=0.000, availability_factor=1.00, wacc_real=0.07, lifetime_years=80, notes="Water availability is handled by the hydro module, not this static factor."),
    "HydSC": dict(technology="hydro_storage", fuel="", commodity_price_variable="", co2_emissions_t_per_mwh_th=0.000, availability_factor=1.00, wacc_real=0.07, lifetime_years=80, notes="Reservoir storage/inflows/cascades are handled by the hydro module."),
    "Solar": dict(technology="solar", fuel="", commodity_price_variable="", co2_emissions_t_per_mwh_th=0.000, availability_factor=1.00, wacc_real=0.07, lifetime_years=30, notes="Hourly resource availability is provided by the VRE profile."),
    "Wind": dict(technology="wind", fuel="", commodity_price_variable="", co2_emissions_t_per_mwh_th=0.000, availability_factor=1.00, wacc_real=0.07, lifetime_years=30, notes="Hourly resource availability is provided by the VRE profile."),
}

# These fallback years are LOCAL MODELLING ASSUMPTIONS, not EDGS data.
# They are used only when both EDGS commissioning-year fields are blank.
STATUS_FALLBACK_YEAR = {
    "under construction": "BASE_PLUS_1",
    "fully consented": 2030,
    "applied for consent": 2030,
    "announced": 2035,
    "generic": 2035,
    "potential": 2040,
    "early stages": 2040,
    "consent lapsed": 2040,
}


# -----------------------------------------------------------------------------
# Generic helpers
# -----------------------------------------------------------------------------

def _ascii(value: object) -> str:
    return unicodedata.normalize("NFKD", str(value)).encode("ascii", "ignore").decode("ascii")


def slugify(value: object) -> str:
    text = _ascii(value).strip().lower().replace("&", " and ")
    # config.py reads CSVs with comment="#". Never allow a literal hash in an
    # identifier because it would truncate the remainder of that CSV row.
    text = text.replace("#", " no ")
    text = text.replace("'", "")
    for ch in ["/", "-", ",", "(", ")", ":", "."]:
        text = text.replace(ch, " ")
    return "_".join(text.split())


def norm_text(value: object) -> str:
    return " ".join(_ascii(value).strip().lower().replace("–", "-").split())


def safe_num(value: object) -> float:
    return pd.to_numeric(pd.Series([value]), errors="coerce").iloc[0]


def parse_bool(value: object, default: bool = True) -> bool:
    if value is None or pd.isna(value):
        return default
    text = str(value).strip().lower()
    if text in {"true", "1", "yes", "y", "t"}:
        return True
    if text in {"false", "0", "no", "n", "f"}:
        return False
    raise ValueError(f"Invalid boolean value: {value!r}")


def annuity_factor(wacc: float, lifetime_years: float) -> float:
    """Capital-recovery factor for a real WACC and economic lifetime."""
    if lifetime_years <= 0:
        raise ValueError("lifetime_years must be > 0")
    if abs(wacc) < 1e-12:
        return 1.0 / lifetime_years
    return wacc / (1.0 - (1.0 + wacc) ** (-lifetime_years))


def ensure_columns(df: pd.DataFrame, required: Iterable[str], source: str) -> None:
    missing = [c for c in required if c not in df.columns]
    if missing:
        raise ValueError(f"{source} is missing required columns: {missing}")


def unique_asset_ids(df: pd.DataFrame) -> pd.Series:
    base = (
        df["technology"].astype(str)
        + "_"
        + df["zone"].astype(str)
        + "_"
        + df["plant_slug"].astype(str)
    )
    out = base.copy()
    duplicates = base.duplicated(keep=False)
    if duplicates.any():
        suffix = df.groupby(base).cumcount() + 1
        out.loc[duplicates] = base.loc[duplicates] + "_" + suffix.loc[duplicates].astype(str)
    return out


# -----------------------------------------------------------------------------
# Input tables
# -----------------------------------------------------------------------------

def load_generation_stack(path: Path) -> pd.DataFrame:
    df = pd.read_excel(path, sheet_name=GENERATION_SHEET)
    required = [
        "Scenario", "Plant", "Status", "PlantType", "Tech", "TechName",
        "Substation", "Region", "Capacity (MW)", "Heat Rate (GJ/GWh)",
        "Variable operating costs (NZD/MWh)",
        "Fixed operating costs (NZD/kW/year)",
        "Fuel delivery costs (NZD/GJ)", "Capital cost (NZD/kW)",
        "Connection cost (NZD $m)", "Total Capital costs (NZD $m)",
        "Earliest Commissioning Year", "Fixed Commissioning Year",
    ]
    ensure_columns(df, required, GENERATION_SHEET)
    return df


def load_commodity_prices(path: Path) -> pd.DataFrame:
    df = pd.read_excel(path, sheet_name=COMMODITY_SHEET)
    required = ["TimePeriod", "Variable", "Scenario", "Value"]
    ensure_columns(df, required, COMMODITY_SHEET)
    df = df.copy()
    df["TimePeriod"] = pd.to_numeric(df["TimePeriod"], errors="coerce").astype("Int64")
    df["Value"] = pd.to_numeric(df["Value"], errors="coerce")
    df["_variable_norm"] = df["Variable"].map(norm_text)
    df["_scenario_norm"] = df["Scenario"].map(norm_text)
    return df


def technology_assumptions_table() -> pd.DataFrame:
    """Return the explicit EDGS-tech-to-model assumption table embedded above."""
    rows = []
    for edgs_tech, values in TECHNOLOGY_ASSUMPTIONS.items():
        row = {"edgs_tech": edgs_tech, **values}
        row["financial_assumption_status"] = "PROVISIONAL"
        rows.append(row)
    df = pd.DataFrame(rows)
    if df["edgs_tech"].duplicated().any():
        raise ValueError("TECHNOLOGY_ASSUMPTIONS contains duplicate EDGS technology codes")
    return df


def load_region_map(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path, comment="#")
    required = ["edgs_region", "zone_11", "zone_14", "zone_16"]
    ensure_columns(df, required, str(path))
    df = df.copy()
    df["_region_norm"] = df["edgs_region"].map(slugify)
    if df["_region_norm"].duplicated().any():
        raise ValueError("Region mapping contains duplicate EDGS regions after normalisation")
    return df


def validate_against_nodes(df: pd.DataFrame, nodes_path: Path | None) -> None:
    if nodes_path is None:
        return
    nodes = pd.read_csv(nodes_path, comment="#")
    ensure_columns(nodes, ["zone"], str(nodes_path))
    if "enabled" in nodes.columns:
        enabled = nodes["enabled"].map(lambda x: parse_bool(x, default=True))
        nodes = nodes.loc[enabled].copy()
    node_zones = set(nodes["zone"].astype(str).str.strip())
    mapped_zones = set(df["zone"].astype(str).str.strip())
    missing = sorted(mapped_zones - node_zones)
    if missing:
        raise ValueError(
            "Generation assets map to zones not present in the selected nodes.csv: "
            + ", ".join(missing)
        )


# -----------------------------------------------------------------------------
# EDGS commodity-price lookup and cost calculations
# -----------------------------------------------------------------------------

def lookup_commodity_price(
    commodity_df: pd.DataFrame,
    variable: str,
    scenario: str,
    year: int,
) -> float:
    if not str(variable).strip():
        return np.nan
    mask = (
        commodity_df["_variable_norm"].eq(norm_text(variable))
        & commodity_df["_scenario_norm"].eq(norm_text(scenario))
        & commodity_df["TimePeriod"].eq(int(year))
    )
    rows = commodity_df.loc[mask, "Value"].dropna()
    if len(rows) == 0:
        raise ValueError(
            f"No EDGS commodity-price value for variable={variable!r}, "
            f"scenario={scenario!r}, year={year}."
        )
    if len(rows) > 1 and not np.allclose(rows.to_numpy(float), float(rows.iloc[0])):
        raise ValueError(
            f"Multiple inconsistent EDGS commodity prices for {variable!r}, {scenario!r}, {year}."
        )
    return float(rows.iloc[-1])


def derive_efficiency(row: pd.Series) -> float:
    """Return a PyPSA generator efficiency.

    For fuel-burning plants, EDGS heat rate is converted to efficiency.
    For non-fuel technologies, efficiency is set to 1 because the electricity
    output is the primary modeled energy carrier; hydrological/VRE availability
    is handled separately.
    """
    fuel_raw = row.get("fuel", "")
    fuel = "" if fuel_raw is None or pd.isna(fuel_raw) else str(fuel_raw).strip()
    hr_gj_per_gwh = safe_num(row.get("Heat Rate (GJ/GWh)"))
    if fuel and np.isfinite(hr_gj_per_gwh) and hr_gj_per_gwh > 0:
        # Some EDGS cogeneration rows use accounting heat rates below the
        # thermodynamic 3.6 GJ/MWh threshold. Preserve the EDGS heat rate for
        # fuel/carbon accounting but cap the PyPSA electricity efficiency at 1.
        return float(np.clip(3600.0 / float(hr_gj_per_gwh), 1e-9, 1.0))
    return 1.0


def plant_overnight_capex_nzd_per_mw(row: pd.Series) -> float:
    """Use EDGS Total Capital costs where possible, otherwise rebuild it."""
    cap_mw = safe_num(row.get("Capacity (MW)"))
    total_m = safe_num(row.get("Total Capital costs (NZD $m)"))
    if np.isfinite(cap_mw) and cap_mw > 0 and np.isfinite(total_m) and total_m >= 0:
        return float(total_m) * 1e6 / float(cap_mw)

    base_kw = safe_num(row.get("Capital cost (NZD/kW)"))
    conn_m = safe_num(row.get("Connection cost (NZD $m)"))
    components = []
    if np.isfinite(base_kw):
        components.append(float(base_kw) * 1000.0)
    if np.isfinite(conn_m) and np.isfinite(cap_mw) and cap_mw > 0:
        components.append(float(conn_m) * 1e6 / float(cap_mw))
    return float(sum(components)) if components else np.nan


def annualized_capital_cost_nzd_per_mw_year(row: pd.Series) -> float:
    """Annualised investment + fixed O&M for candidate capacity.

    WACC and lifetime are explicit study assumptions defined in TECHNOLOGY_ASSUMPTIONS, not inferred from EDGS.
    """
    overnight = plant_overnight_capex_nzd_per_mw(row)
    fixed_om_kw_yr = safe_num(row.get("Fixed operating costs (NZD/kW/year)"))
    wacc = safe_num(row.get("wacc_real"))
    life = safe_num(row.get("lifetime_years"))

    if not np.isfinite(overnight):
        # Some existing/current plants have no investment cost in EDGS. This is
        # acceptable because existing capacity is non-extendable and sunk.
        return 0.0
    if not np.isfinite(wacc) or not np.isfinite(life):
        raise ValueError(
            f"Missing WACC/lifetime for plant {row.get('Plant')!r} ({row.get('technology')})."
        )

    annualized = overnight * annuity_factor(float(wacc), float(life))
    if np.isfinite(fixed_om_kw_yr):
        annualized += float(fixed_om_kw_yr) * 1000.0
    return float(annualized)


def build_srmc_columns(
    df: pd.DataFrame,
    commodity_df: pd.DataFrame,
    scenario: str,
    planning_years: list[int],
) -> pd.DataFrame:
    out = df.copy()
    carbon_prices = {
        y: lookup_commodity_price(commodity_df, DEFAULT_CARBON_VARIABLE, scenario, y)
        for y in planning_years
    }

    for year in planning_years:
        marginal = []
        fuel_price_col = []
        fuel_component_col = []
        carbon_component_col = []

        for _, row in out.iterrows():
            vom = safe_num(row.get("Variable operating costs (NZD/MWh)"))
            vom = 0.0 if not np.isfinite(vom) else float(vom)
            delivery = safe_num(row.get("Fuel delivery costs (NZD/GJ)"))
            delivery = 0.0 if not np.isfinite(delivery) else float(delivery)
            hr_gj_per_gwh = safe_num(row.get("Heat Rate (GJ/GWh)"))
            heat_rate_gj_per_mwh = (
                float(hr_gj_per_gwh) / 1000.0
                if np.isfinite(hr_gj_per_gwh) and hr_gj_per_gwh > 0
                else 0.0
            )

            variable_raw = row.get("commodity_price_variable", "")
            variable = "" if variable_raw is None or pd.isna(variable_raw) else str(variable_raw).strip()
            if variable:
                wholesale = lookup_commodity_price(commodity_df, variable, scenario, year)
                fuel_component = (wholesale + delivery) * heat_rate_gj_per_mwh
            else:
                wholesale = np.nan
                # If no commodity-price series is assigned, retain any EDGS delivery
                # cost as an explicit variable-cost component rather than inventing a
                # wholesale fuel price.
                fuel_component = delivery * heat_rate_gj_per_mwh

            ef_th = safe_num(row.get("co2_emissions_t_per_mwh_th"))
            # Carbon is calculated directly from the EDGS heat rate. The
            # technology assumption is tCO2/MWh_th, equivalent to /3.6 tCO2/GJ.
            # This is preferable to using the PyPSA efficiency for CHP rows,
            # where EDGS may report an accounting heat rate below 3.6 GJ/MWh.
            if np.isfinite(ef_th) and ef_th != 0.0 and heat_rate_gj_per_mwh > 0:
                emissions_t_per_mwh_el = heat_rate_gj_per_mwh * float(ef_th) / 3.6
                carbon_component = carbon_prices[year] * emissions_t_per_mwh_el
            else:
                carbon_component = 0.0

            total = vom + fuel_component + carbon_component
            marginal.append(float(total))
            fuel_price_col.append(wholesale)
            fuel_component_col.append(float(fuel_component))
            carbon_component_col.append(float(carbon_component))

        out[f"wholesale_fuel_price_nzd_per_gj_{year}"] = fuel_price_col
        out[f"fuel_cost_nzd_per_mwh_{year}"] = fuel_component_col
        out[f"carbon_cost_nzd_per_mwh_{year}"] = carbon_component_col
        out[f"marginal_cost_{year}"] = marginal

    # Backward-compatible field for config.py. The current config must later be
    # updated to select marginal_cost_<model_year> for multi-year simulations.
    out["marginal_cost"] = out[f"marginal_cost_{planning_years[0]}"]
    return out


# -----------------------------------------------------------------------------
# Commissioning logic
# -----------------------------------------------------------------------------

def _fallback_year(status: str, base_year: int) -> int | None:
    value = STATUS_FALLBACK_YEAR.get(status)
    if value is None:
        return None
    if value == "BASE_PLUS_1":
        return int(base_year) + 1
    return int(value)


def resolve_commissioning(
    row: pd.Series,
    policy: str,
    base_year: int,
) -> dict:
    status = norm_text(row.get("Status", ""))
    earliest = safe_num(row.get("Earliest Commissioning Year"))
    fixed = safe_num(row.get("Fixed Commissioning Year"))
    earliest = int(earliest) if np.isfinite(earliest) else None
    fixed = int(fixed) if np.isfinite(fixed) else None

    if status == "current":
        return {
            "commissioning_type": "existing",
            "available_from_year": np.nan,
            "earliest_commissioning_year": np.nan,
            "fixed_commissioning_year": np.nan,
            "commissioning_year_source": "edgs_status_current",
        }

    if policy == "edgs_strict":
        if fixed is not None:
            return {
                "commissioning_type": "fixed",
                "available_from_year": fixed,
                "earliest_commissioning_year": np.nan,
                "fixed_commissioning_year": fixed,
                "commissioning_year_source": "edgs_fixed_commissioning_year",
            }
        if earliest is not None:
            return {
                "commissioning_type": "flexible",
                "available_from_year": earliest,
                "earliest_commissioning_year": earliest,
                "fixed_commissioning_year": np.nan,
                "commissioning_year_source": "edgs_earliest_commissioning_year",
            }

    # Recommended policy for a historical/base-year PyPSA calibration:
    # - only Status=Current is existing in the base year;
    # - Under construction is committed/fixed, but not before base_year+1;
    # - other non-current projects are investment candidates, not forced builds.
    if policy == "status_first":
        if status == "under construction":
            source_year = fixed if fixed is not None else earliest
            year = max(int(base_year) + 1, source_year or int(base_year) + 1)
            source = "edgs_year_shifted_after_base_year" if source_year is not None else "assumed_from_status_under_construction"
            return {
                "commissioning_type": "fixed",
                "available_from_year": year,
                "earliest_commissioning_year": np.nan,
                "fixed_commissioning_year": year,
                "commissioning_year_source": source,
            }

        source_year = earliest if earliest is not None else fixed
        if source_year is None:
            source_year = _fallback_year(status, base_year)
            source = f"assumed_from_status_{slugify(status)}" if source_year is not None else "missing_no_rule"
        else:
            source = (
                "edgs_earliest_commissioning_year"
                if earliest is not None
                else "edgs_fixed_commissioning_year_treated_as_candidate_availability"
            )

        if source_year is None:
            return {
                "commissioning_type": "candidate_without_year",
                "available_from_year": np.nan,
                "earliest_commissioning_year": np.nan,
                "fixed_commissioning_year": np.nan,
                "commissioning_year_source": source,
            }

        year = max(int(base_year) + 1, int(source_year))
        if year != int(source_year):
            source += ";shifted_after_base_year"
        return {
            "commissioning_type": "flexible",
            "available_from_year": year,
            "earliest_commissioning_year": year,
            "fixed_commissioning_year": np.nan,
            "commissioning_year_source": source,
        }

    # edgs_strict fallback when both explicit years are missing
    fallback = _fallback_year(status, base_year)
    if fallback is None:
        return {
            "commissioning_type": "candidate_without_year",
            "available_from_year": np.nan,
            "earliest_commissioning_year": np.nan,
            "fixed_commissioning_year": np.nan,
            "commissioning_year_source": "missing_no_rule",
        }
    commissioning_type = "fixed" if status == "under construction" else "flexible"
    return {
        "commissioning_type": commissioning_type,
        "available_from_year": fallback,
        "earliest_commissioning_year": fallback if commissioning_type == "flexible" else np.nan,
        "fixed_commissioning_year": fallback if commissioning_type == "fixed" else np.nan,
        "commissioning_year_source": f"assumed_from_status_{slugify(status)}",
    }


# -----------------------------------------------------------------------------
# Overrides and output formatting
# -----------------------------------------------------------------------------

def apply_plant_overrides(df: pd.DataFrame, path: Path | None) -> pd.DataFrame:
    if path is None or not path.exists():
        return df
    overrides = pd.read_csv(path, comment="#")
    if overrides.empty:
        return df
    ensure_columns(overrides, ["plant_slug"], str(path))
    overrides = overrides.copy()
    overrides["plant_slug"] = overrides["plant_slug"].astype(str).str.strip()
    if overrides["plant_slug"].duplicated().any():
        raise ValueError("Plant override file contains duplicate plant_slug rows")

    out = df.merge(overrides, on="plant_slug", how="left", suffixes=("", "__override"))
    for col in list(df.columns):
        ocol = f"{col}__override"
        if ocol in out.columns:
            mask = out[ocol].notna() & out[ocol].astype(str).str.strip().ne("")
            out.loc[mask, col] = out.loc[mask, ocol]
            out = out.drop(columns=[ocol])

    # Apply override-only columns that are valid model fields.
    valid_extra = {
        "decommissioning_year", "enabled", "override_notes", "override_source",
    }
    for col in overrides.columns:
        if col == "plant_slug" or col in df.columns:
            continue
        ocol = f"{col}__override" if f"{col}__override" in out.columns else col
        if col in valid_extra or col.startswith("marginal_cost_"):
            if ocol in out.columns and ocol != col:
                out[col] = out[ocol]
                out = out.drop(columns=[ocol])
    return out


def convert_currency(df: pd.DataFrame, output_currency: str, nzd_per_usd: float | None) -> pd.DataFrame:
    if output_currency.upper() == "NZD":
        out = df.copy()
        out["model_currency"] = "NZD"
        return out
    if output_currency.upper() != "USD":
        raise ValueError("output_currency must be NZD or USD")
    if nzd_per_usd is None or not np.isfinite(nzd_per_usd) or nzd_per_usd <= 0:
        raise ValueError("USD output requires a positive --nzd-per-usd exchange rate")

    out = df.copy()
    cost_cols = [
        c for c in out.columns
        if c == "capital_cost"
        or c == "marginal_cost"
        or c.startswith("marginal_cost_")
        or c.startswith("fuel_cost_nzd_per_mwh_")
        or c.startswith("carbon_cost_nzd_per_mwh_")
        or c.startswith("wholesale_fuel_price_nzd_per_gj_")
    ]
    for col in cost_cols:
        out[col] = pd.to_numeric(out[col], errors="coerce") / float(nzd_per_usd)
    out["model_currency"] = "USD"
    out["nzd_per_usd_used"] = float(nzd_per_usd)
    return out


def build_capacity_table(args: argparse.Namespace) -> pd.DataFrame:
    xlsx = Path(args.input_xlsx)
    region_path = Path(args.region_map)
    nodes_path = Path(args.nodes) if args.nodes else None
    overrides_path = Path(args.plant_overrides) if args.plant_overrides else None

    stack = load_generation_stack(xlsx)
    commodity = load_commodity_prices(xlsx)
    tech = technology_assumptions_table()
    regions = load_region_map(region_path)

    selected = stack.loc[
        stack["Scenario"].astype(str).map(norm_text).eq(norm_text(args.scenario))
    ].copy()
    if selected.empty:
        available = sorted(stack["Scenario"].dropna().astype(str).unique().tolist())
        raise ValueError(f"Scenario {args.scenario!r} not found. Available: {available}")

    # Technology mapping is explicit and auditable; never infer it from a slug.
    selected = selected.merge(tech, left_on="Tech", right_on="edgs_tech", how="left", validate="many_to_one")
    if selected["technology"].isna().any():
        missing = sorted(selected.loc[selected["technology"].isna(), "Tech"].astype(str).unique())
        raise ValueError(f"Missing technology assumptions for EDGS Tech codes: {missing}")

    # Spatial mapping is independent from the EDGS source rows.
    selected["_region_norm"] = selected["Region"].map(slugify)
    selected = selected.merge(regions, on="_region_norm", how="left", validate="many_to_one")
    zone_col = f"zone_{int(args.topology)}"
    if zone_col not in selected.columns:
        raise ValueError(f"Unsupported topology: {args.topology}")
    selected["zone"] = selected[zone_col]
    if selected["zone"].isna().any():
        missing = sorted(selected.loc[selected["zone"].isna(), "Region"].astype(str).unique())
        raise ValueError(f"Unmapped EDGS regions for topology {args.topology}: {missing}")

    # Stable plant identifier.
    selected["plant"] = (
        selected["Plant"].astype(str).str.strip().str.replace("#", "No. ", regex=False)
    )
    selected["plant_slug"] = selected["Plant"].map(slugify)
    selected["generator"] = selected["technology"].astype(str)

    # Numeric source fields and model efficiency.
    selected["installed_capacity"] = pd.to_numeric(selected["Capacity (MW)"], errors="coerce").fillna(0.0)
    selected["efficiency"] = selected.apply(derive_efficiency, axis=1)
    hr_mwh = pd.to_numeric(selected["Heat Rate (GJ/GWh)"], errors="coerce") / 1000.0
    ef_th = pd.to_numeric(selected["co2_emissions_t_per_mwh_th"], errors="coerce").fillna(0.0)
    selected["co2_emissions_t_per_mwh_el"] = (hr_mwh.fillna(0.0) * ef_th / 3.6).clip(lower=0.0)
    selected["availability_factor"] = pd.to_numeric(selected["availability_factor"], errors="coerce").fillna(1.0)
    geothermal_mask = selected["technology"].eq("geothermal")
    selected.loc[geothermal_mask, "availability_factor"] = float(args.geothermal_availability_factor)
    selected.loc[geothermal_mask, "notes"] = (
        "Geothermal availability_factor set by --geothermal-availability-factor "
        f"({float(args.geothermal_availability_factor):.3f}); this is a study assumption, not an EDGS field."
    )
    selected["p_min_mw"] = 0.0

    # Commissioning status is deliberately separated from the EDGS source fields.
    commissioning = selected.apply(
        lambda r: pd.Series(resolve_commissioning(r, args.commissioning_policy, args.base_year)),
        axis=1,
    )
    selected = pd.concat([selected.reset_index(drop=True), commissioning.reset_index(drop=True)], axis=1)

    # Existing/fixed capacity is sunk in a one-year PyPSA investment objective.
    # Candidate capacity receives annualised investment + fixed O&M.
    candidate_cost = selected.apply(annualized_capital_cost_nzd_per_mw_year, axis=1)
    selected["overnight_capex_nzd_per_mw"] = selected.apply(plant_overnight_capex_nzd_per_mw, axis=1)
    selected["annualized_capital_cost_nzd_per_mw_year"] = candidate_cost
    selected["capital_cost"] = np.where(
        selected["commissioning_type"].eq("flexible"),
        candidate_cost,
        0.0,
    )

    planning_years = [int(y) for y in args.planning_years]
    selected = build_srmc_columns(selected, commodity, args.scenario, planning_years)

    # Preserve useful source/audit fields in stable output names.
    selected["status"] = selected["Status"].astype(str)
    selected["plant_type"] = selected["PlantType"].astype(str)
    selected["tech"] = selected["Tech"].astype(str)
    selected["tech_name"] = selected["TechName"].astype(str)
    selected["substation"] = selected["Substation"].astype(str)
    selected["region"] = selected["Region"].astype(str)
    selected["edgs_scenario"] = selected["Scenario"].astype(str)
    selected["fixed_operating_cost_nzd_per_kw_year"] = pd.to_numeric(
        selected["Fixed operating costs (NZD/kW/year)"], errors="coerce"
    )
    selected["variable_operating_cost_nzd_per_mwh"] = pd.to_numeric(
        selected["Variable operating costs (NZD/MWh)"], errors="coerce"
    )
    selected["fuel_delivery_cost_nzd_per_gj"] = pd.to_numeric(
        selected["Fuel delivery costs (NZD/GJ)"], errors="coerce"
    )
    selected["heat_rate_gj_per_gwh"] = pd.to_numeric(selected["Heat Rate (GJ/GWh)"], errors="coerce")
    selected["edgs_capital_cost_nzd_per_kw"] = pd.to_numeric(selected["Capital cost (NZD/kW)"], errors="coerce")
    selected["edgs_connection_cost_nzd_m"] = pd.to_numeric(selected["Connection cost (NZD $m)"], errors="coerce")
    selected["edgs_total_capital_cost_nzd_m"] = pd.to_numeric(selected["Total Capital costs (NZD $m)"], errors="coerce")
    selected["edgs_earliest_commissioning_year"] = pd.to_numeric(
        selected["Earliest Commissioning Year"], errors="coerce"
    ).astype("Int64")
    selected["edgs_fixed_commissioning_year"] = pd.to_numeric(
        selected["Fixed Commissioning Year"], errors="coerce"
    ).astype("Int64")
    selected["topology"] = int(args.topology)
    selected["requires_hydro_asset"] = selected["technology"].astype(str).str.startswith("hydro_")
    selected["requires_vre_profile"] = selected["technology"].isin(["wind", "solar"])
    selected["commissioning_policy"] = args.commissioning_policy
    selected["builder_version"] = SCRIPT_VERSION
    selected["enabled"] = ~selected["commissioning_type"].eq("candidate_without_year")

    selected["asset_id"] = unique_asset_ids(selected)
    selected = apply_plant_overrides(selected, overrides_path)
    validate_against_nodes(selected.loc[selected["enabled"].map(lambda x: parse_bool(x, True))], nodes_path)
    selected = convert_currency(selected, args.output_currency, args.nzd_per_usd)

    # Clean year-like model columns after overrides.
    for col in [
        "available_from_year", "earliest_commissioning_year", "fixed_commissioning_year", "decommissioning_year"
    ]:
        if col in selected.columns:
            selected[col] = pd.to_numeric(selected[col], errors="coerce").round(0).astype("Int64")

    # Keep model-required fields first, followed by audit/source fields.
    first = [
        "asset_id", "generator", "zone", "technology", "capital_cost", "marginal_cost",
        "efficiency", "installed_capacity", "p_min_mw", "availability_factor",
        "available_from_year", "earliest_commissioning_year", "fixed_commissioning_year",
        "commissioning_type", "commissioning_year_source", "decommissioning_year", "enabled",
    ]
    first = [c for c in first if c in selected.columns]
    year_cost_cols = []
    for y in planning_years:
        year_cost_cols.extend([
            f"marginal_cost_{y}",
            f"wholesale_fuel_price_nzd_per_gj_{y}",
            f"fuel_cost_nzd_per_mwh_{y}",
            f"carbon_cost_nzd_per_mwh_{y}",
        ])
    year_cost_cols = [c for c in year_cost_cols if c in selected.columns]
    rest = [c for c in selected.columns if c not in first + year_cost_cols and not c.startswith("_")]
    out = selected[first + year_cost_cols + rest].copy()
    out = out.sort_values(["zone", "technology", "plant", "asset_id"]).reset_index(drop=True)
    return out


def build_capacity_output(master: pd.DataFrame) -> pd.DataFrame:
    """Return the model capacity table with no duplicated economic fields.

    ``cost_key`` is deliberately plant-specific and equals ``asset_id``. This
    preserves EDGS connection- and plant-specific costs in the separate
    generation costs extract without collapsing them to technology averages.
    """
    out = master.copy()
    out["cost_key"] = out["asset_id"].astype(str)
    columns = [c for c in CAPACITY_OUTPUT_COLUMNS if c in out.columns]
    out = out[columns].copy()

    if out["asset_id"].duplicated().any():
        duplicates = sorted(out.loc[out["asset_id"].duplicated(False), "asset_id"].unique())
        raise ValueError(f"Capacity output contains duplicate asset_id values: {duplicates}")
    if out["cost_key"].duplicated().any():
        raise ValueError("Capacity output contains duplicate cost_key values")
    return out


def _cost_note(row: pd.Series) -> str:
    pieces = [
        f"EDGS plant={row.get('plant', '')}",
        f"status={row.get('status', '')}",
        "capital_cost is the legacy annualised NZ generator value (investment + fixed O&M for flexible candidates; zero for sunk existing/fixed capacity)",
    ]
    variable_om = safe_num(row.get("variable_operating_cost_nzd_per_mwh"))
    overnight = safe_num(row.get("overnight_capex_nzd_per_mw"))
    if np.isfinite(variable_om):
        pieces.append(f"variable_O&M_NZD_per_MWh={float(variable_om):.6f}")
    if np.isfinite(overnight):
        pieces.append(f"overnight_CAPEX_NZD_per_MW={float(overnight):.6f}")
    return "; ".join(pieces) + "."


def build_generation_costs_extract(
    master: pd.DataFrame,
    planning_years: list[int],
    output_currency: str,
) -> pd.DataFrame:
    """Return plant-specific generation rows ready to append to costs.csv.

    The table is long by model year because EDGS fuel and carbon prices produce
    a different marginal cost in each planning year. Each ``cost_key`` therefore
    has one row per year, which is the format expected by config.py's cost-year
    resolver.

    ``capital_cost`` remains the supported legacy annualised override. Raw EDGS
    CAPEX, fixed O&M and other audit inputs remain in explicit traceability
    columns/notes; the code does not silently reinterpret their units.
    """
    currency = output_currency.upper()
    capital_unit = f"{currency}/MW-year"
    marginal_unit = f"{currency}/MWh_e"
    rows: list[dict] = []

    for _, asset in master.iterrows():
        for year in planning_years:
            marginal_col = f"marginal_cost_{int(year)}"
            if marginal_col not in master.columns:
                raise ValueError(f"Missing {marginal_col} required for the costs extract")

            row = {column: np.nan for column in COSTS_OUTPUT_COLUMNS}
            row.update(
                {
                    "cost_key": str(asset["asset_id"]),
                    "technology": str(asset["technology"]),
                    "year": int(year),
                    "base_year": int(year),
                    "nominal_basis": "MW_e generator capacity",
                    "capital_cost": safe_num(asset.get("capital_cost")),
                    "capital_cost_unit": capital_unit,
                    "marginal_cost": safe_num(asset.get(marginal_col)),
                    "marginal_cost_unit": marginal_unit,
                    "efficiency": safe_num(asset.get("efficiency")),
                    "co2_emissions": safe_num(asset.get("co2_emissions_t_per_mwh_el")),
                    "fixed_operating_cost_nzd_per_kw_year": safe_num(
                        asset.get("fixed_operating_cost_nzd_per_kw_year")
                    ),
                    "fuel_delivery_cost_nzd_per_gj": safe_num(asset.get("fuel_delivery_cost_nzd_per_gj")),
                    "heat_rate_gj_per_gwh": safe_num(asset.get("heat_rate_gj_per_gwh")),
                    "connection_cost_nzd_m": safe_num(asset.get("edgs_connection_cost_nzd_m")),
                    "total_capital_costs_nzd_m": safe_num(asset.get("edgs_total_capital_cost_nzd_m")),
                    "source": "MBIE EDGS 2024 assumptions workbook + explicit study assumptions in build_capacity_EDGS.py",
                    "notes": _cost_note(asset),
                }
            )
            rows.append(row)

    out = pd.DataFrame(rows, columns=COSTS_OUTPUT_COLUMNS)
    if out.duplicated(["cost_key", "year"]).any():
        duplicates = out.loc[out.duplicated(["cost_key", "year"], False), ["cost_key", "year"]]
        raise ValueError(f"Generation costs extract contains duplicate cost_key/year rows:\n{duplicates}")

    required = ["capital_cost", "marginal_cost", "efficiency"]
    missing = out[required].isna().any(axis=1)
    if missing.any():
        bad = out.loc[missing, ["cost_key", "year", *required]]
        raise ValueError(f"Generation costs extract has missing required values:\n{bad}")
    return out


def build_no_new_thermal_variant(capacity: pd.DataFrame) -> tuple[pd.DataFrame, list[str]]:
    """Disable new thermal candidates while preserving every other field."""
    out = capacity.copy()
    enabled = out["enabled"].map(lambda value: parse_bool(value, default=True))
    is_new = ~out["commissioning_type"].eq("existing")
    disable = enabled & is_new & out["technology"].isin(NEW_THERMAL_TECHNOLOGIES)
    disabled_assets = out.loc[disable, "asset_id"].astype(str).tolist()
    out.loc[disable, "enabled"] = False
    return out, disabled_assets


def derive_related_output_path(capacity_path: Path, kind: str) -> Path:
    """Derive companion filenames while respecting an explicit capacity path."""
    stem = capacity_path.stem
    if kind == "costs":
        if stem.startswith("generators_capacity"):
            suffix = stem[len("generators_capacity"):]
            companion_stem = f"costs_generation{suffix}"
        else:
            companion_stem = f"{stem}_costs_generation"
    elif kind == "no_new_thermal":
        companion_stem = f"{stem}_no_new_thermal"
    else:
        raise ValueError(f"Unsupported companion output kind: {kind}")
    return capacity_path.with_name(companion_stem + capacity_path.suffix)


def write_audit_files(df: pd.DataFrame, output_path: Path) -> None:
    audit_dir = output_path.parent / f"{output_path.stem}_audit"
    audit_dir.mkdir(parents=True, exist_ok=True)

    unresolved = df.loc[df["commissioning_type"].eq("candidate_without_year")].copy()
    unresolved.to_csv(audit_dir / "unresolved_commissioning.csv", index=False, float_format=CSV_FLOAT_FORMAT)

    status_summary = (
        df.groupby(["status", "commissioning_type", "commissioning_year_source"], dropna=False)
        .agg(plants=("asset_id", "count"), capacity_mw=("installed_capacity", "sum"))
        .reset_index()
    )
    status_summary.to_csv(audit_dir / "commissioning_summary.csv", index=False, float_format=CSV_FLOAT_FORMAT)

    zone_summary = (
        df.groupby(["zone", "technology"], dropna=False)
        .agg(plants=("asset_id", "count"), capacity_mw=("installed_capacity", "sum"))
        .reset_index()
    )
    zone_summary.to_csv(audit_dir / "capacity_summary_by_zone_technology.csv", index=False, float_format=CSV_FLOAT_FORMAT)

    source_cols = [
        "asset_id", "plant", "status", "tech", "substation", "region", "zone",
        "edgs_earliest_commissioning_year", "edgs_fixed_commissioning_year",
        "commissioning_type", "available_from_year", "commissioning_year_source",
        "overnight_capex_nzd_per_mw", "wacc_real", "lifetime_years",
        "financial_assumption_status", "notes",
    ]
    source_cols = [c for c in source_cols if c in df.columns]
    df[source_cols].to_csv(audit_dir / "plant_assumption_register.csv", index=False, float_format=CSV_FLOAT_FORMAT)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build plant-level PyPSA generation inputs from the MBIE EDGS 2024 assumptions workbook."
    )
    parser.add_argument(
        "--input-xlsx",
        default=str(BASE_DIR / "electricity-demand-generation-scenarios-2024-assumptions.xlsx"),
    )
    parser.add_argument("--scenario", default=DEFAULT_SCENARIO)
    parser.add_argument("--topology", type=int, choices=[11, 14, 16], default=14)
    parser.add_argument("--nodes", default=None, help="Optional nodes.csv used to validate the selected topology.")
    parser.add_argument(
        "--region-map",
        default=str(BASE_DIR / "edgs_region_to_zone.csv"),
        help="CSV mapping EDGS Region to zone_11/zone_14/zone_16.",
    )
    parser.add_argument(
        "--plant-overrides",
        default=None,
        help="Optional plant-level overrides CSV. Keep source corrections outside the EDGS workbook.",
    )
    parser.add_argument(
        "--planning-years",
        nargs="+",
        type=int,
        default=DEFAULT_PLANNING_YEARS,
    )
    parser.add_argument("--base-year", type=int, default=DEFAULT_BASE_YEAR)
    parser.add_argument(
        "--commissioning-policy",
        choices=["status_first", "edgs_strict"],
        default="status_first",
        help=(
            "status_first: only Status=Current is existing in the base year; non-current projects are "
            "candidates/committed after the calibration year. edgs_strict: preserve EDGS fixed-year semantics."
        ),
    )
    parser.add_argument(
        "--geothermal-availability-factor",
        "--geothermal-capacity-factor",
        dest="geothermal_availability_factor",
        type=float,
        default=DEFAULT_GEOTHERMAL_AVAILABILITY_FACTOR,
        help=(
            "Geothermal technical availability exported in generators_capacity.csv. "
            "The legacy --geothermal-capacity-factor option remains an alias. Default: 0.85."
        ),
    )
    parser.add_argument(
        "--output",
        default=None,
        help=(
            "Optional generators_capacity.csv path. By default writes to "
            "data_NZ_generation/outputs/ using scenario and topology in the filename."
        ),
    )
    parser.add_argument(
        "--costs-output",
        default=None,
        help=(
            "Optional generation-only costs extract path. By default it is derived "
            "from --output, e.g. generators_capacity.csv -> costs_generation.csv."
        ),
    )
    parser.add_argument(
        "--no-new-thermal-output",
        default=None,
        help=(
            "Optional no-new-thermal capacity path. By default it is derived from "
            "--output, e.g. generators_capacity.csv -> generators_capacity_no_new_thermal.csv."
        ),
    )
    parser.add_argument(
        "--skip-no-new-thermal",
        action="store_true",
        help="Do not write the automatically derived no-new-thermal capacity variant.",
    )
    parser.add_argument("--output-currency", choices=["NZD", "USD"], default="NZD")
    parser.add_argument(
        "--nzd-per-usd",
        type=float,
        default=None,
        help="Required only when --output-currency USD. No hidden exchange-rate assumption is used.",
    )
    args = parser.parse_args()
    if not 0.0 < float(args.geothermal_availability_factor) <= 1.0:
        parser.error("--geothermal-availability-factor must be greater than 0 and at most 1")
    return args


def main() -> None:
    args = parse_args()
    if args.output:
        output_path = Path(args.output)
    else:
        output_path = BASE_DIR / "outputs" / f"generators_capacity_{slugify(args.scenario)}_{int(args.topology)}.csv"
    costs_output_path = (
        Path(args.costs_output)
        if args.costs_output
        else derive_related_output_path(output_path, "costs")
    )
    no_new_thermal_path = (
        Path(args.no_new_thermal_output)
        if args.no_new_thermal_output
        else derive_related_output_path(output_path, "no_new_thermal")
    )

    for path in [output_path, costs_output_path, no_new_thermal_path]:
        path.parent.mkdir(parents=True, exist_ok=True)

    master = build_capacity_table(args)
    capacity = build_capacity_output(master)
    generation_costs = build_generation_costs_extract(
        master,
        [int(year) for year in args.planning_years],
        args.output_currency,
    )

    capacity.to_csv(output_path, index=False, float_format=CSV_FLOAT_FORMAT)
    generation_costs.to_csv(costs_output_path, index=False, float_format=CSV_FLOAT_FORMAT)
    write_audit_files(master, output_path)

    disabled_thermal_assets: list[str] = []
    if not args.skip_no_new_thermal:
        no_new_thermal, disabled_thermal_assets = build_no_new_thermal_variant(capacity)
        no_new_thermal.to_csv(no_new_thermal_path, index=False, float_format=CSV_FLOAT_FORMAT)

    enabled = capacity["enabled"].map(lambda x: parse_bool(x, True))
    geothermal = capacity["technology"].eq("geothermal")
    print("=" * 80)
    print("EDGS -> PyPSA generation preprocessing")
    print("=" * 80)
    print(f"Builder version:       {SCRIPT_VERSION}")
    print(f"EDGS scenario:         {args.scenario}")
    print(f"Topology:              {args.topology} regions/nodes")
    print(f"Commissioning policy:  {args.commissioning_policy}")
    print(f"Planning years:        {args.planning_years}")
    print(f"Output currency:       {args.output_currency}")
    print(f"Plant/project rows:    {len(capacity):,}")
    print(f"Enabled rows:          {int(enabled.sum()):,}")
    print(f"Unique zones:          {capacity.loc[enabled, 'zone'].nunique()}")
    print(f"Installed/candidate MW:{capacity.loc[enabled, 'installed_capacity'].sum():,.1f}")
    print(f"Geothermal availability:{float(args.geothermal_availability_factor):.3f} ({int(geothermal.sum())} assets)")
    print(f"Capacity output:       {output_path}")
    print(f"Generation costs:      {costs_output_path}")
    if not args.skip_no_new_thermal:
        print(f"No-new-thermal output: {no_new_thermal_path}")
        print(f"New thermal disabled:  {len(disabled_thermal_assets)} assets")
    print("Technology assumptions: embedded in build_capacity_EDGS.py and documented in the Supplementary Method")
    print(f"Audit folder:          {output_path.parent / (output_path.stem + '_audit')}")
    print("\nIMPORTANT: append/merge the generation costs extract into the general costs.csv;")
    print("do not retain older generation rows with the same cost_key/year pairs.")
    print("The downstream config.py must apply availability_factor to every generator:")
    print("multiply hourly resource/hydro profiles by it, or use it as constant p_max_pu otherwise.")


if __name__ == "__main__":
    main()

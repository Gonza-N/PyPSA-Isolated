"""Run only the seven sequential Magallanes scenarios; explicit --run required."""
# Cambio: ejecuta sólo los siete escenarios secuenciales definidos en el notebook.
# No genera figuras ni ejecuta la robustez meteorológica.
import argparse
import json
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true", help="Execute the full sequential optimizations.")
    parser.add_argument("--output-root", default="output/output_magallanes_costos_gonzalo_v2_20260924")
    args = parser.parse_args()
    root = Path(__file__).resolve().parent
    if Path.cwd().resolve() != root:
        parser.error(f"Run from the repository root: {root}")
    if not args.run:
        print("No optimization executed. Use --run for 7 scenarios x 2025/2030/2040/2050.")
        print(f"Output: {args.output_root}. Existing bundles are preserved.")
        return
    notebook = json.loads((root / "case_analysis_Magallanes.ipynb").read_text(encoding="utf-8"))
    namespace = {"__name__": "__magallanes_sequential__"}
    # Source markers protect against accidental cell reordering.
    expected = {2: "import config", 3: "RUN_SIMULATIONS", 7: "years =",
                9: "def run_selected_scenarios"}
    for index in (2, 3):
        marker = expected[index]
        source = "".join(notebook["cells"][index]["source"])
        if marker not in source:
            raise RuntimeError(f"Notebook layout changed at cell {index}; review the runner.")
        exec(compile(source, f"case_analysis_Magallanes.ipynb:cell{index}", "exec"), namespace)
    namespace["RUN_SIMULATIONS"] = True
    namespace["OUTPUT_ROOT"] = Path(args.output_root)
    namespace["SIMULATION_EXPORT_DIR"] = namespace["OUTPUT_ROOT"] / "simulations"
    namespace["TABLE_EXPORT_DIR"] = namespace["OUTPUT_ROOT"] / "tables"
    namespace["PAPER_FIGURE_DIR"] = namespace["OUTPUT_ROOT"] / "figures"
    namespace["ROBUSTNESS_EXPORT_DIR"] = namespace["OUTPUT_ROOT"] / "robustness"
    for key in ("OUTPUT_ROOT", "SIMULATION_EXPORT_DIR", "TABLE_EXPORT_DIR",
                "PAPER_FIGURE_DIR", "ROBUSTNESS_EXPORT_DIR"):
        namespace[key].mkdir(parents=True, exist_ok=True)
    for index in (7, 9):
        marker = expected[index]
        source = "".join(notebook["cells"][index]["source"])
        if marker not in source:
            raise RuntimeError(f"Notebook layout changed at cell {index}; review the runner.")
        exec(compile(source, f"case_analysis_Magallanes.ipynb:cell{index}", "exec"), namespace)
    print("Sequential run finished. Economics are in each bundle and manifest.csv.")
    print("Open the notebook with RUN_SIMULATIONS=False for tables and figures.")


if __name__ == "__main__":
    main()

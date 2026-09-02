"""Centralized data configuration for dataset generation and runs.

Edit this file to change data source and random data parameters.
Both run.py and generate_data.py will import from here.
"""

DATA_CONFIG = {
    # Data source mode for run.py: "example" | "json" | "random"
    "mode": "json",

    # When mode == "json": path to the dataset file
    "json_path": "random_data.json",

    # Output path used by generate_data.py
    "out": "random_data.json",

    # Parameters for random data generation
    "random": {
        "num_nodes":2,
        "num_services": 3,
        "num_agents": 10,
        "combos_per_node": 6,
        "avg_services_per_combo": 3.0,
        "min_req_services": 2,
        "max_req_services": 3,
        "capacity_low": 50,
        "capacity_high": 150,
        "req_pattern": "random",  # "random" | "cycle" | "heavy_first"
        "seed": 42,
        "drop_dominated": True,    # filter dominated combos if True

        # 新增：用比例調整
        "fl_ratio": 0.15,
        "fh_ratio": 0.30
    },
}

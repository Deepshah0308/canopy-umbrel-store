# Working on Canopy

Use the ponytail skill at level full for all work in this repo: the smallest complete change, no extras nobody asked for.

Before shipping: `python3 tools/lint_store.py`, `python3 tools/build_site.py`, `python3 -m pytest -q`, and install changed apps with `sudo -E python3 tools/umbrel_sim.py <app-id>`.

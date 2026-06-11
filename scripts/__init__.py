# Package marker so `scripts.fuzz` is importable from the repo root.
# (The standalone helper scripts in this dir are still run path-based; this
# marker does not change that — it only enables `python -m scripts.fuzz` and
# `from scripts.fuzz import ...` in the test suite.)

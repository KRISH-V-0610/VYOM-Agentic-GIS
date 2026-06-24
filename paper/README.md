# VYOM — Publication package

This folder contains everything needed to submit the VYOM work to a GeoAI journal.

```
paper/
├── VYOM_manuscript.md      ← THE PAPER (single self-contained manuscript; all figures embedded)
├── README.md               ← this file
├── figures/                ← 14 publication figures (PNG, 200 dpi) — regenerable
├── results/
│   ├── benchmark_results.json   ← full per-query agent traces
│   └── benchmark_summary.csv    ← one row per benchmark query
└── scripts/
    ├── make_figures.py     ← regenerates all 14 figures from the live PostGIS DB + real COGs
    └── run_benchmark.py     ← reruns the 10-query agentic tool-use benchmark
```

## Read the paper
Open [VYOM_manuscript.md](VYOM_manuscript.md). It covers, in order: abstract → introduction →
related work → study area & dataset → methods/architecture (ARD pipeline, PostGIS catalogue,
MCP tool servers, agent orchestration) → implementation/clients → experiments & results
(catalogue flood signal, raster products, agentic benchmark) → discussion → limitations →
conclusion. All 14 figures are embedded inline.

## Reproduce every figure and number
From the project root (`D:\AGENTIC-GIS`), with PostgreSQL/PostGIS running:

```bash
# 1. all 14 figures from the live catalogue + real ARD COGs
PYTHONPATH=src PYTHONIOENCODING=utf-8 ./myenv/Scripts/python.exe paper/scripts/make_figures.py

# 2. the agentic tool-use benchmark (needs an LLM backend key in .env)
PYTHONPATH=src PYTHONIOENCODING=utf-8 ./myenv/Scripts/python.exe paper/scripts/run_benchmark.py
```

Nothing in the manuscript is hand-typed or synthetic: every table and figure traces to the
PostGIS catalogue (88 Kerala-2018 scenes, 1,455 cached metrics) or to the recorded benchmark
traces.

# GECKO

GECKO co-evolves property graphs and query features to test graph database logic.
It combines boundary-value mining, closed-AST query synthesis, an independent
reference interpreter, and explanation-aware search feedback.

[GitHub](https://github.com/nf202/GECKO) · [中文说明](README.zh-CN.md) · [Architecture](docs/ARCHITECTURE.md) ·
[Runtime setup](docs/RUNTIME.md) · [Reproduction](docs/REPRODUCING.md)

This source release accompanies *Co-Evolving Property Graphs and Query Features
for Logic Bug Detection in Graph Database Systems*. It contains the finalized
identity-fixed, transport2 campaign implementation. Historical pilots, paper
drafts, raw experiment runs, baseline repositories, and database binaries are
excluded. This is a source artifact; the code retains the directory layout used
by the experiments.

## Quick start: no database or model required

Python 3.11 or later is required. From this directory:

```sh
python -m venv .venv
# Linux/macOS:
source .venv/bin/activate
# Windows PowerShell: .venv\Scripts\Activate.ps1
python -m pip install -e ".[test]"
python scripts/check_reference.py
python -m pytest
python scripts/doctor.py
python tools/verify_manifest.py
```

`check_reference.py` admits the example features, emits Cypher and Gremlin, and
computes expected rows in Python. It does not connect to databases or call a
model. `doctor.py` reports optional runtime prerequisites without starting any
processes; missing database runtimes are expected in a source-only checkout.
`verify_manifest.py` verifies the shipped files and reports unexpected files.

## Database campaigns

The experiment launchers require **Linux**, project-local database runtimes,
Java, and (for generation) an authenticated model worker. See the runtime and
reproduction guides before running campaigns. Install Python clients with:

```sh
python -m pip install -e ".[engines]"
python scripts/gecko_transport2_entry_20261109.py --help
```

Always use the transport2 entry above. It installs the fixture serialization
adapters used by the finalized experiment runs. Output must be a new directory
under this checkout's `runs/`. Campaigns create isolated local instances and
reset their own test graphs. They are intended for test machines.

## Repository layout

| Directory | Contents |
| --- | --- |
| `scripts/` | Final runner, feature grammar, evolution, queue and engine adapters |
| `src/gqltest/` | Reference semantics and shared graph/engine support |
| `tests/` | Offline regressions for semantics, compilation and mutation invariants |
| `examples/` | Small graph and closed query features for the offline demonstration |
| `configs/` | Single-seed experiment arguments and ablation variants |
| `docs/` | Reproduction, environment requirements and source provenance |
| `runtime/` | User-provided binaries and JARs; only its README is shipped |

The algorithms remain in their experiment modules to preserve source snapshots
and imports. Use an editable install from a full checkout for campaigns; a
standalone wheel is not a complete campaign distribution.

## Publication status

This tree is prepared for source publication. A redistribution license and final
citation metadata still need to be supplied by the authors. No
license is granted by the absence of a `LICENSE` file. Experiment results and
vendor issue adjudication are maintained separately; this README makes no new
bug-count claim. See [publication notes](docs/PUBLICATION.md).

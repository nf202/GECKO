# Implementation map

GECKO keeps the experiment module layout. The `gqltest` package name is retained
for import compatibility; the research system is called GECKO.

| Component | Implementation |
| --- | --- |
| Final campaign entry | `scripts/gecko_transport2_entry_20261109.py` |
| Fixture transport adapters | `scripts/gecko_transport2_codec_20261109.py` |
| Graph/feature search, feedback and triage | `scripts/campaign_evolving_graph.py` |
| Graph mutations and limits | `scripts/graph_evolution.py`, `scripts/fixture_shapes.py` |
| Closed AST typing, Cypher compilation and interpretation | `scripts/gecko_ast.py` |
| Gremlin semantic profile and compilation | `scripts/gecko_gremlin.py` |
| Model prompts, schemas and sanitization | `scripts/gecko_llm.py` |
| Optional feature mutation and fitness | `scripts/gecko_evolve.py` |
| Persistent feature IDs and result binding | `scripts/gecko_identity.py` |
| Model queue and workstation worker | `scripts/llm_queue.py`, `scripts/local_llm_worker.py` |
| Cypher engine launch/support | `scripts/campaign_current_path_semantics.py`, `scripts/campaign_broad_semantic.py`, `src/gqltest/engines.py` |
| FalkorDB adapter | `scripts/falkordb_frontier_adapter.py` |
| Embedded Gremlin engine bridge | `scripts/gremlin_engines.py`, `scripts/java/GremlinBatchRunner.java` |
| Fixed feature catalogs | `scripts/campaign_single_seed_frontier.py`, `scripts/semantic_*frontier*.py` |
| Reference fixture/state support | `src/gqltest/semantic_suite.py`, `scripts/semantic_write_frontier.py` |

The files with historical campaign names also provide shared launcher and
catalog functions. They are dependencies of the final runner; their older
standalone `main` functions are not the supported entry for the paper artifact.

## Semantics and evidence

Features are JSON ASTs, not executable model-generated programs. Admission checks
typing, the declared semantic subset, discriminative value and row budgets.
Reference rows are computed directly on graph objects in Python. The Cypher and
Gremlin profiles differ, including their treatment of null/missing values and
arithmetic. Their interpreters should not be substituted for each other.

Default `--contract v1` and optional `--contract v2` expose different contracts.
Feature mutation and repair are separately opt-in. The shipped single-seed
configurations preserve the default contract and feature-evolution flags.

The runner freezes fixtures, queries, reference rows and source hashes for each
trajectory. `known_label` records previously triaged signatures; a matching
signature or a stable discrepancy alone does not establish a new vendor defect.
Run outputs must be adjudicated separately before inclusion in paper counts.

## Release-only environment changes

Three copied source files receive environment changes:

1. `gecko_runtime_guard.py`: import `fcntl` when a lock is acquired, allowing
   reference-only imports on Windows. Campaign execution still requires Linux.
2. `campaign_broad_semantic.py`: replace a private libpython path with optional
   `GECKO_LIBPYTHON_PRELOAD`, relevant to the historical Memgraph 3.13.2 launcher.
3. `local_llm_worker.py`: accept the SSH host, remote runs root, Node executable
   and Codex CLI entry as arguments; quote remote paths and validate queue IDs.
   Prompt generation, schemas and response content are preserved.

All exported text uses UTF-8 and LF line endings. `source-provenance.json`
records original and release hashes, normalization and environment changes.
No source changes were made to AST semantics, admission thresholds, mutations,
scoring, feature identities or transport adapters during release preparation.

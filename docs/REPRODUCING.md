# Running a single trajectory

First complete [runtime setup](RUNTIME.md). The supported final entry is
`scripts/gecko_transport2_entry_20261109.py`; do not invoke the underlying
campaign directly, because that bypasses the fixture transport adapters.

## Example command

From the checkout root on the Linux experiment host:

```sh
python scripts/gecko_transport2_entry_20261109.py \
  --output "$PWD/runs/gecko-E-example" \
  --seed 20261102 --children 3 --generations 1000 \
  --hours 12 --max-llm-calls 100 --gremlin \
  --generate --mine-values
```

This is the E argument set. `configs/single-seed.json` records the shared
arguments and five variants from the final transport2 supervisor:

| Variant | Arguments added to the common budget |
| --- | --- |
| D | `--start dense30 --no-mutation --generate` |
| E | `--generate --mine-values` |
| F | `--generate --mine-values --selection random` |
| Eraw | `--generate --mine-values --score raw` |
| Enodisc | `--generate --mine-values --no-discriminative` |

These are separate single-seed configurations. The JSON is a parameter record,
not an automatically executed multi-run driver. `--output` must point to a new
directory under this checkout's `runs/`. Failed outputs are retained; use a new
directory for a deliberately authorized new attempt.

The campaign's `--hours` deadline is checked cooperatively between work units.
The original formal supervisor additionally enforced a 43,200-second process
deadline, recorded partial generations and checked model failures. That
machine-specific supervisor is not shipped. The example above does not provide
the same hard process deadline, and should not be presented as an exact replay
of the supervisor policy.

## Model worker

The original worker invokes an installed Codex CLI on a workstation that already
has its own authentication. It exchanges only requests/responses over SSH. It
does not copy credentials into this tree. Configure SSH and the local CLI first,
then run the worker in a separate terminal while the campaign is running:

```sh
python scripts/local_llm_worker.py \
  --host YOUR_SSH_DESTINATION \
  --remote-root /absolute/path/to/GECKO/runs \
  --run /absolute/path/to/GECKO/runs/gecko-E-example \
  --node /absolute/path/to/node \
  --cli /absolute/path/to/codex/bin/codex.js \
  --model YOUR_EXPERIMENT_MODEL --effort YOUR_EXPERIMENT_EFFORT \
  --max-calls 100
```

On Windows, pass Windows paths for `--node` and `--cli`, and keep the remote paths
as Linux paths. If omitted, Node is discovered on PATH and the CLI location
defaults to the historical per-user npm installation. Explicit model/effort
arguments freeze the choice; otherwise the worker reads the local Codex config.
The worker disables tools/search and configured MCP servers. Exact model access
and CLI flags depend on the workstation's installed CLI. Record its version and
the experiment model settings alongside results.

The queue contract is:

- Request: `runs/<run>/llm/req_<id>.json`, with `id`, `kind`, `prompt`, `schema`
  and `prompt_sha256`.
- Response: `resp_<id>.json`, with `status`, `output`, the same `prompt_sha256`
  and model/usage metadata. `status: "ok"` carries a JSON object matching the
  requested schema.
- Write a temporary response then rename it atomically. Failed responses are
  retained and are not silently retried. The campaign checks prompt binding.

Model prompts contain graph summaries and the closed AST grammar, but not
expected query results. Retain prompts, responses and usage when documenting a
new run. A new stochastic model response is not a byte-for-byte replay of a
historical experiment, even with the same graph RNG seed.

## Interpreting outputs

The runner writes protocol/source snapshots, graph and catalog artifacts,
model-queue evidence, generation history and summaries. Engine ownership
receipts describe the processes it launches. Keep these with their source and
runtime hashes. Candidate signals and known-signature labels need separate
semantic adjudication before being called newly discovered bugs.

The source artifact does not include the paper's complete baseline tools,
known-bug replay collection, result aggregation or raw evidence archive.
Reproducing paper tables therefore requires those separately curated materials;
the commands here reproduce the GECKO search implementation and configurations.

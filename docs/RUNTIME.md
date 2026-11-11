# Campaign runtime requirements

The source release does not download, redistribute or provision database
distributions. Use Linux for database campaigns. Install the Python engine
clients with `python -m pip install -e ".[engines]"`. The optional `legacy`
extra installs Kuzu for older helper campaigns; Kuzu is not a current GECKO target.

Run `python scripts/doctor.py` for presence checks, or add `--require` to fail
when any prerequisite is missing. This does not validate versions, hashes,
shared libraries, kernel namespace support or successful engine startup.

## Frozen launcher layout

All paths below are relative to the checkout unless explicitly absolute.

| Engine | Required files |
| --- | --- |
| Neo4j 2026.09.0 | `runtime/neo4j_2026.09.0_all.deb`; extracted `runtime/neo4j-2026.09.0-extracted/usr/share/neo4j/bin/neo4j` |
| Memgraph 3.14.0 | executable wrapper `runtime/jammy-bwrap-3.14.0/memgraph` and its referenced runtime |
| FalkorDB 6.0.3 | `runtime/jammy-bwrap/rootfs`; `runtime/falkordb-6.0.3-redis/redis-8.10.2/src/redis-server`; `runtime/falkordb-6.0.3-inputs/falkordb-amazonlinux2023-x64.so`; `/usr/bin/bwrap` |
| TinkerGraph 3.8.1 | complete compatible distribution JARs under `runtime/tinkerpop-3.8.1/lib/` |
| TinkerGraph 3.7.3 / JanusGraph 1.1.0 | complete compatible dependency JARs listed in `runtime/janusgraph/classpath.txt` |

Neo4j requires a JDK supported by its distribution. The Gremlin bridge needs
both `java` and `javac` on PATH, with a JDK compatible with the selected JARs.
Provision these according to the original engine distributions. Do not combine
TinkerPop 3.7.3 and 3.8.1 JAR sets on one classpath.

`classpath.txt` is a single colon-separated line of absolute JAR paths on Linux,
with no shell quoting. Regenerate it for the new checkout location. TinkerGraph
3.7.3 uses the JanusGraph classpath; TinkerGraph 3.8.1 uses its separate directory.
The Java runner is compiled on demand into the ignored `runtime/gremlin-runner/`.

The FalkorDB wrapper uses bubblewrap, a compatible Jammy root filesystem and
project paths bind-mounted into that filesystem. The Memgraph wrapper must
provide the executable and libraries required by that version. A binary placed
at the expected path alone is insufficient. Original namespace wrappers and
root filesystems are environment artifacts and are not shipped here.

For exact package checks, the frozen Neo4j package digest is
`583b3b64fde62696d349e0aed6047e374e83695bf25ec3427dfa5eaac7a7f35a`.
Other runtime identities should be checked against the original experiment
ownership receipts before claiming exact reproduction. This source tree does
not contain those runtime distributions or all their checksums.

## Historical recall mode

`--engine-set recall` selects the older affected releases in the runner, including
Neo4j 2025.02.0 at `/usr/share/neo4j/bin/neo4j` and Memgraph 3.13.2 through
`runtime/jammy-bwrap/memgraph`. Those releases need their own runtimes. If the
Memgraph 3.13.2 environment needs a libpython preload, set
`GECKO_LIBPYTHON_PRELOAD` to that environment's library. There is no private
machine path fallback in this release.

## Reproduction boundary

Offline tests exercise reference semantics and compilation on source examples.
They do not establish a successful replay on a clean database host. Full replay
also requires the original engine versions, runtime wrappers, model settings,
budget policy and frozen inputs. No database installation or model invocation
is performed by the default test suite or GitHub Actions workflow.

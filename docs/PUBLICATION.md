# Source publication notes

The GitHub tree contains an allowlisted copy of the final GECKO implementation,
its required shared modules, offline tests, examples and documentation. The
working experiment directory and paper were not reorganized in place.

`source-provenance.json` records the source hashes before export and after
environment patches / UTF-8 LF normalization. Six final deployment files were
checked against the 2026-11-09 identity-fixed deployment manifest. Two required
shared modules (`path_reference.py` and `predicate.py`) were recovered read-only
from the experiment host and checked against their recorded SHA-256 values.

Excluded materials include database binaries/JARs/root filesystems, raw runs,
workstation relay logs, credentials, private connection settings, historical
pilots, third-party baseline repositories, paper drafts and older submission
bundles. These exclusions keep the source artifact distinct from the separately
curated experimental evidence.

Before public publication the authors need to supply:

- The redistribution license and copyright metadata. No license has been chosen
  automatically, and this tree intentionally has no `LICENSE` file yet.
- Final citation metadata. The repository is https://github.com/nf202/GECKO.
  No DOI or venue acceptance has been invented.
- Any separately released paper-result / bug-reproduction artifacts and their
  access links, if the repository is intended to reproduce all paper tables.

The source hashes in `MANIFEST.sha256.json` describe the initial prepared tree.
If maintainers subsequently change files, regenerate the manifest as part of a
deliberate new release. `tools/verify_manifest.py --strict` checks both file
integrity and unexpected files, excluding documented local build/runtime output.

Offline verification is distinct from full engine replay. See `RUNTIME.md` and
`REPRODUCING.md` for the limits of this source artifact. No GitHub repository is
created, pushed or published by the preparation process.

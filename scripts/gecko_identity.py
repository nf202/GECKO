"""Pure identity checks and durable proposal allocation; no engines or models."""
import json
import os
from pathlib import Path


class IdentityError(ValueError):
    """Structural identity corruption must abort, never become an engine error."""


def unique_index(records, key="id", context="batch", expected=None):
    result = {}
    for record in records:
        ident = record[key]
        if not isinstance(ident, str) or not ident:
            raise IdentityError(f"{context}: invalid {key}: {ident!r}")
        if ident in result:
            raise IdentityError(f"{context}: duplicate {key}: {ident}")
        result[ident] = record
    if expected is not None and set(result) != set(expected):
        raise IdentityError(f"{context}: missing/unexpected result ids")
    return result


class FeatureIds:
    """Reserve before admission; persist rejected/evicted ids too.

    No state may be inferred from the surviving pool: rejected and evicted ids
    would be lost. A populated pool requires its full allocation checkpoint.
    This helper validates identity only; it does not implement campaign resume.
    """
    def __init__(self, path, pool=()):
        self.path = Path(path)
        pool = list(pool)
        unique_index(pool, key="fid", context="resume pool")
        if self.path.exists():
            state = json.loads(self.path.read_text(encoding="utf-8"))
            self.used = state["issued"]
            unique_index([{"id": x} for x in self.used], context="issued checkpoint")
            numeric = [int(x) for x in self.used if x.isascii() and x.isdecimal()]
            if len(numeric) != len(self.used) or len(set(numeric)) != len(numeric):
                raise ValueError("invalid/aliased numeric ids in checkpoint")
            self.next = state["next_fid"]
            if not isinstance(self.next, int) or self.next < max(numeric, default=0) + 1:
                raise ValueError("allocation checkpoint would reuse an id")
            if any(f["fid"] not in self.used for f in pool):
                raise ValueError("pool fid absent from full allocation checkpoint")
        else:
            if pool:
                raise ValueError("cannot resume populated pool without allocation history")
            self.used, self.next = [], 1
            self._save()

    def _save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump({"schema": "gecko-feature-ids/1", "next_fid": self.next,
                       "issued": self.used}, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.path)

    def reserve(self, count):
        if not isinstance(count, int) or isinstance(count, bool) or count < 0:
            raise ValueError("proposal count must be a nonnegative integer")
        start = self.next
        self.used.extend(f"{n:04d}" for n in range(start, start + count))
        self.next += count
        self._save()  # Before parsing, rejecting, admitting or evicting.
        return start


def parse_reserved(output, parse_features, ids):
    if output is None:
        output = {}  # Preserve existing failed/empty-response handling.
    if not isinstance(output, dict) or not isinstance(output.get("features", []), list):
        raise ValueError("model feature envelope must contain a list")
    count = len(output.get("features", []))
    start = ids.reserve(count)
    features, errors = parse_features(output, start)
    unique_index(features, key="fid", context="parsed proposals")
    if len(features) + len(errors) != count:
        raise ValueError("proposal parser lost an ordinal")
    return features, errors

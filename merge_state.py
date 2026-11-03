"""Merge two v4 state.json files into the first.

Used by the workflow's Save state step when its push is rejected because
another run pushed first. Git can't merge state.json line by line (both runs
rewrite the same keys), so this merges by meaning instead: anything either run
saw counts as seen, keeping the later day.

    python3 merge_state.py state.json ours.json   # writes merged into state.json
"""
import json
import sys


def load(path):
    try:
        with open(path) as f:
            d = json.load(f)
    except (OSError, ValueError):
        return {}
    return d if isinstance(d, dict) and d.get("version") == 4 else {}


def latest(a, b):
    out = dict(a)
    for k, t in b.items():
        if k not in out or t > out[k]:
            out[k] = t
    return out


def merge(theirs, ours):
    seen = {}
    for src in set(theirs.get("seen", {})) | set(ours.get("seen", {})):
        seen[src] = latest(theirs.get("seen", {}).get(src, {}),
                           ours.get("seen", {}).get(src, {}))
    return {"version": 4, "seen": seen,
            "roles": latest(theirs.get("roles", {}), ours.get("roles", {})),
            "boards": latest(theirs.get("boards", {}), ours.get("boards", {}))}


def main(target, other):
    theirs, ours = load(target), load(other)
    if not ours:              # our copy is unreadable: keep what's pushed
        return
    merged = merge(theirs, ours) if theirs else ours
    with open(target, "w") as f:
        json.dump(merged, f, indent=0, sort_keys=True)


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])

"""Presentation-only demo: print one snapshot as JSON to stdout (no file is written)."""

import json

from collector import collect_system_snapshot

if __name__ == "__main__":
    print(json.dumps(collect_system_snapshot(), indent=2))

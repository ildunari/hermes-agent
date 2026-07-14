#!/usr/bin/env python3
"""Validate Phase-5 eval corpus shape without invoking a model."""
import json
from pathlib import Path


def main() -> int:
    value=json.loads(Path(__file__).with_name("cases.json").read_text())
    cases=value.get("cases",[]); ids=[case.get("id") for case in cases]
    required={"attribution","privacy","gate","transport","diagnostics"}
    assert value.get("schema")==1 and len(ids)==len(set(ids)) and all(ids)
    assert required <= {case.get("suite") for case in cases}
    print(json.dumps({"cases":len(cases),"suites":sorted(required),"valid":True},sort_keys=True))
    return 0
if __name__ == "__main__": raise SystemExit(main())

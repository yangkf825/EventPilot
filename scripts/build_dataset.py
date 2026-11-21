#!/usr/bin/env python3
"""Rebuild or validate the frozen reviewed English dataset (no model API)."""
import argparse
import json
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from eventpilot.compiler import build, validate

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--data',type=Path,default=ROOT/'data')
    p.add_argument('--validate-only',action='store_true')
    a=p.parse_args()
    result=validate(a.data) if a.validate_only else build(a.data)
    print(json.dumps(result,ensure_ascii=False,indent=2))

if __name__=='__main__':main()

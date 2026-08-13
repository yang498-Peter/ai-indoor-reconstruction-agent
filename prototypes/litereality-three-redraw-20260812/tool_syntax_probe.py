#!/usr/bin/env python3
"""Capability probe: compile the reconstruction generator without importing optional runtimes."""

from pathlib import Path


def main() -> None:
    source = Path(__file__).with_name("generate_demo.py")
    compile(source.read_text(encoding="utf-8"), str(source), "exec")
    print("TOOL_SYNTAX_PROBE:PASS")


if __name__ == "__main__":
    main()

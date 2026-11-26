#!/usr/bin/env python3
"""Smoke-test the optional Control-side Hosted Chat dependencies offline."""

import importlib
import importlib.util
import platform
import sys


def main() -> None:
    for name in (
        "httpcore",
        "zstandard",
        "xlrd",
        "pypdf",
        "docx",
        "langgraph.graph",
        "langchain_core",
        "regex",
        "ddgs",
    ):
        importlib.import_module(name)
    if sys.platform == "win32" and platform.machine() == "ARM64":
        # The upstream tokenizer can use conservative byte counts when
        # tiktoken is not available. Do not require a native Rust build.
        importlib.import_module("tzdata")
        assert importlib.util.find_spec("tiktoken") is None
    else:
        importlib.import_module("tiktoken")
    print("Hosted Chat dependency imports passed")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Correct a raw SV600 scan (wrapper; the code lives in the sv600 package)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from sv600.cli import correct_main  # noqa: E402

if __name__ == "__main__":
    correct_main()

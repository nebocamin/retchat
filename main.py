#!/usr/bin/env python3
"""Entry point for Retchat."""
import sys
import os

# Add directory to sys.path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from retchat.app import main

if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Where things live and what is in .env. One place, so every script reads it the same way."""
import os

HERE = os.path.dirname(os.path.abspath(__file__))
ENV = os.path.join(HERE, ".env")


def env(path=None):
    """KEY=value pairs from .env (or `path`). Blank lines and # comments are skipped."""
    e = {}
    path = path or ENV
    if os.path.exists(path):
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    e[k.strip()] = v.strip()
    return e

"""
Minimal .env loader (stdlib only).

Imported first by every entry script so `cp .env.example .env` just works.
Reads KEY=VALUE lines from the repo-root .env (or src/.env). Variables already
set in the real environment always win — the file never overrides them.
"""

import os

_HERE = os.path.dirname(os.path.abspath(__file__))


def load(paths=(os.path.join(_HERE, "..", ".env"), os.path.join(_HERE, ".env"))):
    for path in paths:
        if not os.path.isfile(path):
            continue
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, val = line.split("=", 1)
                key = key.removeprefix("export ").strip()
                val = val.strip()
                if val[:1] in ("'", '"'):
                    val = val[1:].split(val[0], 1)[0]          # quoted: take as-is
                else:
                    val = val.split("#", 1)[0].strip()         # drop inline comment
                if key and val and key not in os.environ:
                    os.environ[key] = val


load()

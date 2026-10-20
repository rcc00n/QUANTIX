"""Lossless, typed JSON DAG with a bounded cache and disk-backed content index.

Only the export representation changes. PostgreSQL source records never change.
Every container is typed, so literal source keys such as r/m/a cannot collide
with reference markers. Decimal values arrive as exact strings from the sanitizer.
"""

import hashlib
import json
import os
import sqlite3
import zlib
from collections import OrderedDict
from json.encoder import encode_basestring

CODEC = "quantix-json-dag-v1"
MIN_NODE_BYTES = 512
CACHE_ENTRIES = 16_384


def compact(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))



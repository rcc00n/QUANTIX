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


class EvidenceStore:
    def __init__(self, path):
        self.path = path
        os.close(os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.executescript(
            "PRAGMA journal_mode=OFF; PRAGMA synchronous=OFF; PRAGMA cache_size=-4096;"
            "PRAGMA temp_store=FILE; PRAGMA mmap_size=0;"
            "CREATE TABLE objects(id INTEGER PRIMARY KEY, digest BLOB UNIQUE NOT NULL, "
            "body BLOB NOT NULL);"
            "CREATE TABLE projections(id INTEGER PRIMARY KEY, name TEXT, body BLOB);"
        )
        self.cache = OrderedDict()
        self.hits = self.nodes = self.original_bytes = 0

    def encode(self, value):
        def node(value):
            if isinstance(value, dict):
                # Reuse each child's canonical JSON in its parent. Re-encoding
                # materialized typed subtrees at every level repeats most work.
                members = ",".join(
                    f"{encode_basestring(key)}:{node(child)}"
                    for key, child in sorted(value.items())
                )
                body = '{"m":{' + members + "}}"
            elif isinstance(value, list):
                body = '{"a":[' + ",".join(map(node, value)) + "]}"
            elif isinstance(value, str):
                return encode_basestring(value)
            else:
                return compact(value)
            if len(body) < MIN_NODE_BYTES:
                return body
            encoded = body.encode("utf-8")
            digest = hashlib.sha256(encoded).digest()
            identifier = self.cache.get(digest)
            if identifier is not None:
                self.cache.move_to_end(digest)
                self.hits += 1
                return f'{{"r":{identifier}}}'
            row = self.db.execute("SELECT id FROM objects WHERE digest=?", (digest,)).fetchone()
            if row is None:
                identifier = self.db.execute(
                    "INSERT INTO objects(digest,body) VALUES (?,?)",
                    (digest, zlib.compress(encoded, level=1)),
                ).lastrowid
                self.nodes += 1
                if self.nodes % 1024 == 0:
                    self.db.commit()
            else:
                identifier = row[0]
                self.hits += 1
            self.cache[digest] = identifier
            if len(self.cache) > CACHE_ENTRIES:
                self.cache.popitem(last=False)
            return f'{{"r":{identifier}}}'

        result = json.loads(node(value)) if isinstance(value, (dict, list)) else value
        return {"$quantix_codec": CODEC, "value": result}

    def objects(self):
        self.db.commit()
        for identifier, digest, body in self.db.execute(
            "SELECT id, hex(digest), body FROM objects ORDER BY id"
        ):
            yield identifier, digest.lower(), zlib.decompress(body).decode("utf-8")

    def lookup(self, identifier):
        row = self.db.execute("SELECT body FROM objects WHERE id=?", (identifier,)).fetchone()
        if row is None:
            raise ValueError("Missing evidence object")
        return zlib.decompress(row[0]).decode("utf-8")

    def projection(self, name, row):
        self.db.execute(
            "INSERT INTO projections(name,body) VALUES (?,?)",
            (name, zlib.compress(compact(row).encode(), 1)),
        )

    def projected_rows(self, name):
        self.db.commit()
        for (body,) in self.db.execute(
            "SELECT body FROM projections WHERE name=? ORDER BY id", (name,)
        ):
            yield json.loads(zlib.decompress(body))

    def close(self):
        self.db.close()



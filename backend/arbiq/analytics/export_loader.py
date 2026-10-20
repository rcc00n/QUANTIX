"""Standalone stdlib loader shipped inside QUANTIX exports. No server access needed.

Usage: python load_export.py archive.zip --dataset opportunities/arbitrage_evaluations.csv
Extract all original CSV fields: add --output reconstructed.csv.
The shared evidence index is disk-backed; never load the entire history into RAM.
"""

import argparse
import csv
import hashlib
import io
import json
import sqlite3
import tempfile
import zipfile
from pathlib import Path

CODEC = "quantix-json-dag-v1"


class ExportReader:
    def __init__(self, archive):
        self.zip = zipfile.ZipFile(archive)
        self.manifest = json.loads(self.zip.read("manifest.json"))
        self.directory = tempfile.TemporaryDirectory(prefix="quantix-analysis-")
        self.db = sqlite3.connect(Path(self.directory.name) / "objects.sqlite")
        self.db.executescript(
            "PRAGMA journal_mode=OFF; PRAGMA synchronous=OFF; PRAGMA cache_size=-4096;"
            "CREATE TABLE objects(id INTEGER PRIMARY KEY, body TEXT NOT NULL);"
        )
        if "evidence/objects.jsonl" in self.zip.namelist():
            library_hash = hashlib.sha256()
            with self.zip.open("evidence/objects.jsonl") as stream:
                for line in stream:
                    library_hash.update(line)
                    fields = json.loads(line)
                    identifier, value = fields[0], fields[-1]
                    body = json.dumps(
                        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                    )
                    if len(fields) == 3 and hashlib.sha256(body.encode()).hexdigest() != fields[1]:
                        raise ValueError("Evidence object checksum mismatch")
                    self.db.execute("INSERT INTO objects VALUES (?,?)", (identifier, body))
            self.db.commit()
            expected = next(
                f["sha256"] for f in self.manifest["files"] if f["name"] == "evidence/objects.jsonl"
            )
            if library_hash.hexdigest() != expected:
                raise ValueError("Shared evidence library checksum mismatch")

    def decode(self, value):
        def node(packed, parent=None):
            if not isinstance(packed, dict):
                return packed
            if set(packed) == {"r"}:
                identifier = packed["r"]
                if parent is not None and identifier >= parent:
                    raise ValueError("Invalid cyclic/forward evidence reference")
                row = self.db.execute(
                    "SELECT body FROM objects WHERE id=?", (identifier,)
                ).fetchone()
                if row is None:
                    raise ValueError("Missing evidence object")
                return node(json.loads(row[0]), identifier)
            if set(packed) == {"m"}:
                return {k: node(v, parent) for k, v in packed["m"].items()}
            if set(packed) == {"a"}:
                return [node(v, parent) for v in packed["a"]]
            raise ValueError("Invalid evidence node")

        if isinstance(value, dict) and value.get("$quantix_codec") == CODEC:
            return node(value["value"])
        return value

    def rows(self, name):
        entry = next(f for f in self.manifest["files"] if f["name"] == name)
        json_columns = set(entry.get("json_columns", ()))
        csv.field_size_limit(256 * 1024 * 1024)
        with self.zip.open(name) as stream:
            for row in csv.DictReader(io.TextIOWrapper(stream, encoding="utf-8", newline="")):
                for key in json_columns:
                    if row.get(key):
                        row[key] = self.decode(json.loads(row[key]))
                    else:
                        row[key] = None
                yield row

    def close(self):
        self.db.close()
        self.directory.cleanup()
        self.zip.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive")
    parser.add_argument("--dataset", required=True)
    parser.add_argument("--output")
    args = parser.parse_args()
    reader = ExportReader(args.archive)
    try:
        if args.output:
            with open(args.output, "w", encoding="utf-8", newline="") as stream:
                writer = None
                for row in reader.rows(args.dataset):
                    if writer is None:
                        writer = csv.DictWriter(stream, fieldnames=list(row))
                        writer.writeheader()
                    writer.writerow(
                        {
                            k: json.dumps(v, ensure_ascii=False, separators=(",", ":"))
                            if isinstance(v, (dict, list))
                            else v
                            for k, v in row.items()
                        }
                    )
        else:
            count = sum(1 for _ in reader.rows(args.dataset))
            print(json.dumps({"dataset": args.dataset, "decoded_rows": count}))
    finally:
        reader.close()


if __name__ == "__main__":
    main()

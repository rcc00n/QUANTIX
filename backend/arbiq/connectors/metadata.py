"""Lossless cold public metadata; only active adapter entries stay expanded.

This is an in-process cache, not financial evidence or a new durable database.
Bytes are produced only by this module, never accepted from a request or file.
Restricted decoding additionally forbids arbitrary pickle globals.
"""

import io
import pickle
import zlib
from dataclasses import dataclass
from decimal import Decimal
from itertools import islice

FIELDS = (
    "markets", "markets_by_id", "currencies", "currencies_by_id",
    "baseCurrencies", "quoteCurrencies",
)
MAX_UNCOMPRESSED_BYTES = 256 * 1024 * 1024


class _DataReader(pickle.Unpickler):
    def find_class(self, module, name):
        if (module, name) == ("decimal", "Decimal"):
            return Decimal
        raise ValueError("Cold metadata contains an unsupported value")


def decode(value: bytes, dictionary: bytes = b""):
    if dictionary:
        decompressor = zlib.decompressobj(zdict=dictionary)
        raw = decompressor.decompress(value) + decompressor.flush()
    else:
        raw = zlib.decompress(value)
    return _DataReader(io.BytesIO(raw)).load()


@dataclass(frozen=True)
class MetadataArchive:
    tables: dict[str, dict[str, bytes]]
    uncompressed_bytes: int
    compressed_bytes: int
    dictionaries: dict[str, bytes]

    @classmethod
    def capture(cls, adapter):
        tables = {}
        raw_bytes = packed_bytes = 0
        dictionaries = {}
        for field in FIELDS:
            table = getattr(adapter, field, None)
            if not isinstance(table, dict):
                continue
            packed = {}
            # Reuse a bounded per-table dictionary rather than paying repeated
            # schema/string overhead in every independently addressable entry.
            examples = list(islice(table.values(), 32)) if len(table) >= 64 else []
            dictionary = b"".join(pickle.dumps(v, protocol=5) for v in examples)[-32768:]
            dictionaries[field] = dictionary
            packed_bytes += len(dictionary)
            for key, value in table.items():
                if not isinstance(key, str):
                    raise ValueError("Unsupported native metadata key")
                raw = pickle.dumps(value, protocol=5)
                raw_bytes += len(raw)
                if raw_bytes > MAX_UNCOMPRESSED_BYTES:
                    raise ValueError("Cold metadata exceeds the configured bound")
                compressor = zlib.compressobj(level=3, zdict=dictionary)
                packed[key] = compressor.compress(raw) + compressor.flush()
                packed_bytes += len(packed[key])
            tables[field] = packed
        if not tables.get("markets"):
            raise ValueError("No native markets to archive")
        return cls(tables, raw_bytes, packed_bytes, dictionaries)

    def get(self, table, key):
        data = self.tables.get(table, {}).get(key)
        return decode(data, self.dictionaries[table]) if data is not None else None

    def focused(self, symbols):
        symbols = set(symbols)
        markets = {symbol: self.get("markets", symbol) for symbol in symbols}
        if any(value is None for value in markets.values()):
            raise ValueError("Selected market is absent from discovery metadata")
        ids = {market["id"] for market in markets.values()}
        codes = {
            market[key] for market in markets.values()
            for key in ("base", "quote", "settle") if market.get(key)
        }
        currencies = {
            code: currency for code in codes
            if (currency := self.get("currencies", code)) is not None
        }
        currency_ids = {c["id"] for c in currencies.values() if c.get("id")}
        result = {
            "markets": markets,
            "markets_by_id": {key: self.get("markets_by_id", key) for key in ids},
            "currencies": currencies,
            "currencies_by_id": {
                key: self.get("currencies_by_id", key) for key in currency_ids
            },
        }
        for field in ("baseCurrencies", "quoteCurrencies"):
            if field in self.tables:
                result[field] = {
                    key: value for key in codes if (value := self.get(field, key)) is not None
                }
        if any(value is None for value in result["markets_by_id"].values()):
            raise ValueError("Native market ID index is incomplete")
        return result

    def restore(self):
        return {
            field: {key: decode(value, self.dictionaries[field]) for key, value in table.items()}
            for field, table in self.tables.items()
        }



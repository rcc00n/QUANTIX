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



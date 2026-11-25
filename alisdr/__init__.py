# The one place the version lives - shown in Settings and `--version`.
__version__ = "1.2.3"


# Existing service overrides and scripts can still pass the former variables.
import os

for _key, _value in tuple(os.environ.items()):
    if _key.startswith("FALCONSDR_"):
        os.environ.setdefault("ALISDR_" + _key[len("FALCONSDR_"):], _value)

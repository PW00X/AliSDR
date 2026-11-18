# The one place the version lives - shown in Settings and `--version`, read by
# packaging/linux-deb/build.sh and packaging/windows/build.ps1.
__version__ = "1.1.1"


# Existing service overrides and scripts can still pass the former variables.
import os

for _key, _value in tuple(os.environ.items()):
    if _key.startswith("FALCONSDR_"):
        os.environ.setdefault("ALISDR_" + _key[len("FALCONSDR_"):], _value)

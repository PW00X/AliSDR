#!/bin/sh
# Build the in-process decoder on the Linux receiver and install it atomically.
set -eu

src=$(CDPATH= cd "$(dirname "$0")" && pwd)
dest="$1"
commit=8b8230526a68d9ec52911dae0bd455cf1bb2fbad
archive_hash=4a8e2ef4cd8caf80cceba80208f956c87eff6542d59c830bbaf93c9660929ac9
stamp=$(cat "$src/install.sh" "$src/bridge.cpp" "$src/upstream-fixes.patch" | sha256sum | cut -d' ' -f1)
if [ -s "$dest/libfalconsdr_dab.so" ] && [ "$(cat "$dest/falconsdr-dab.sha256" 2>/dev/null || true)" = "$stamp" ]; then
    exit 0
fi

sudo_cmd=""
if [ "$(id -u)" != 0 ]; then sudo_cmd=sudo; fi
$sudo_cmd apt-get update -qq
$sudo_cmd apt-get install -y build-essential cmake pkg-config patch python3 libfftw3-dev libfaad-dev zlib1g-dev

tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
curl -fsSL "https://codeload.github.com/JvanKatwijk/dab-cmdline/tar.gz/$commit" -o "$tmp/source.tar.gz"
printf '%s  %s\n' "$archive_hash" "$tmp/source.tar.gz" | sha256sum -c -
tar -xzf "$tmp/source.tar.gz" -C "$tmp"
upstream="$tmp/dab-cmdline-$commit"
patch -d "$upstream" -p1 < "$src/upstream-fixes.patch"
cp "$src/bridge.cpp" "$upstream/library/falconsdr-bridge.cpp"
cat >> "$upstream/library/CMakeLists.txt" <<'EOF'

target_sources(dab_lib PRIVATE falconsdr-bridge.cpp ../devices/device-handler.cpp)
target_compile_definitions(dab_lib PRIVATE DEBUG_ENABLED=0)
target_link_libraries(dab_lib -Wl,--no-undefined)
set_target_properties(dab_lib PROPERTIES OUTPUT_NAME falconsdr_dab)
EOF
case "$(uname -m)" in
    x86_64) set -- -DX64_DEFINED=ON ;;
    *) set -- ;;
esac
cmake -S "$upstream/library" -B "$tmp/build" -DLIB_INSTALL_DIR=lib "$@"
cmake --build "$tmp/build" --parallel 2
python3 - "$tmp/build/libfalconsdr_dab.so" <<'PY'
import ctypes as C
import sys

lib = C.CDLL(sys.argv[1])
Station = C.CFUNCTYPE(None, C.c_uint32, C.c_char_p, C.c_int)
Audio = C.CFUNCTYPE(None, C.POINTER(C.c_int16), C.c_int, C.c_int)
Reception = C.CFUNCTYPE(None, C.c_bool, C.c_int16, C.c_int32)
lib.falcon_dab_open.argtypes = [Station, Audio, Reception]
lib.falcon_dab_open.restype = C.c_void_p
lib.falcon_dab_close.argtypes = [C.c_void_p]
lib.falcon_dab_close.restype = None
station, audio, reception = [callback(lambda *_: None) for callback in (Station, Audio, Reception)]
handle = lib.falcon_dab_open(station, audio, reception)
if not handle:
    raise RuntimeError("The compiled DAB decoder could not start")
lib.falcon_dab_close(handle)
PY
mkdir -p "$dest"
install -m 755 "$tmp/build/libfalconsdr_dab.so" "$dest/libfalconsdr_dab.so.new"
mv -f "$dest/libfalconsdr_dab.so.new" "$dest/libfalconsdr_dab.so"
printf '%s\n' "$stamp" > "$dest/falconsdr-dab.sha256"

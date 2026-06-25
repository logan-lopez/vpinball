#!/bin/bash
#
# Build ONLY bgfx (the single *static* third-party lib) in a given config and
# install its libs into third-party/build-libs/windows-x64, so the MSBuild engine
# can be linked in that config.
#
# Why only bgfx: every other dependency in build-libs is a DLL (BUILD_SHARED=ON),
# so the engine links its config-agnostic *import* lib. Only bgfx is static
# (-DBGFX_LIBRARY_TYPE=STATIC), so its CRT/_ITERATOR_DEBUG_LEVEL must match the
# engine config. Mixing Debug bgfx with a Release engine is the LNK2038 error.
#
# This is a trimmed copy of platforms/windows-x64/external.sh (bgfx section only),
# pinned to the same versions via platforms/config.sh.
#
# Run from a "Developer Command Prompt/PowerShell for VS 2026" (so cmake + MSVC are
# on PATH), from anywhere:
#
#     bash make/build-bgfx-config.sh Release      # default
#     bash make/build-bgfx-config.sh Debug        # to restore the Debug set
#
set -e

CONFIG="${1:-Release}"
REPO="$(cd "$(dirname "$0")/.." && pwd)"
source "${REPO}/platforms/config.sh"

DEST="${REPO}/third-party/build-libs/windows-x64"
WORK="${REPO}/external/windows-x64/${CONFIG}/bgfx"
LIBS=(bgfx64 bimg64 bimg_decode64 bx64)

echo "bgfx ${CONFIG}: pin ${BGFX_CMAKE_VERSION} / patch ${BGFX_PATCH_SHA}"

# 1. Back up whatever static libs are currently installed (so you can switch back
#    without rebuilding). One snapshot dir, only written if it doesn't exist yet.
BK="${REPO}/third-party/build-libs/_bgfx-backup"
if [ ! -d "${BK}" ]; then
   mkdir -p "${BK}"
   for L in "${LIBS[@]}"; do
      [ -f "${DEST}/${L}.lib" ] && cp "${DEST}/${L}.lib" "${BK}/${L}.lib" || true
   done
   echo "Backed up current bgfx libs to ${BK}"
fi

# 2. Fetch bgfx.cmake + the vpinball (vbousquet) bgfx patch, pinned.
rm -rf "${WORK}"; mkdir -p "${WORK}"; cd "${WORK}"
curl -sL "https://github.com/bkaradzic/bgfx.cmake/releases/download/v${BGFX_CMAKE_VERSION}/bgfx.cmake.v${BGFX_CMAKE_VERSION}.tar.gz" -o bgfx.cmake.tar.gz
tar xzf bgfx.cmake.tar.gz
curl -sL "https://github.com/vbousquet/bgfx/archive/${BGFX_PATCH_SHA}.tar.gz" -o bgfx-patch.tar.gz
tar xzf bgfx-patch.tar.gz
cd bgfx.cmake
rm -rf bgfx; mv "../bgfx-${BGFX_PATCH_SHA}" bgfx

# 3. Same OUTPUT_NAME tweaks the official external.sh applies (so libs are *64).
sed -i.bak 's/set_target_properties(bx PROPERTIES FOLDER "bgfx")/set_target_properties(bx PROPERTIES FOLDER "bgfx" OUTPUT_NAME "bx64")/g' cmake/bx/bx.cmake
sed -i.bak 's/set_target_properties(bimg PROPERTIES FOLDER "bgfx")/set_target_properties(bimg PROPERTIES FOLDER "bgfx" OUTPUT_NAME "bimg64")/g' cmake/bimg/bimg.cmake
sed -i.bak 's/set_target_properties(bimg_decode PROPERTIES FOLDER "bgfx")/set_target_properties(bimg_decode PROPERTIES FOLDER "bgfx" OUTPUT_NAME "bimg_decode64")/g' cmake/bimg/bimg_decode.cmake
sed -i.bak 's/set_target_properties(bgfx PROPERTIES FOLDER "bgfx")/set_target_properties(bgfx PROPERTIES FOLDER "bgfx" OUTPUT_NAME "bgfx64")/g' cmake/bgfx/bgfx.cmake

# 4. Configure + build (the MSVC_RUNTIME_LIBRARY generator-expr is what makes the
#    CRT match the chosen config).
cmake -G "Visual Studio 17 2022" -S. \
   -DBGFX_LIBRARY_TYPE=STATIC \
   -DBGFX_BUILD_TOOLS=OFF \
   -DBGFX_BUILD_EXAMPLES=OFF \
   -DBGFX_CONFIG_MULTITHREADED=ON \
   -DBGFX_CONFIG_MAX_FRAME_BUFFERS=256 \
   -DCMAKE_MSVC_RUNTIME_LIBRARY="MultiThreaded\$<\$<CONFIG:Debug>:Debug>" \
   -B build
cmake --build build --config "${CONFIG}"

# 5. Install the static libs into build-libs (overwrites the current set).
cp "build/cmake/bgfx/${CONFIG}/bgfx64.lib"        "${DEST}/"
cp "build/cmake/bimg/${CONFIG}/bimg64.lib"        "${DEST}/"
cp "build/cmake/bimg/${CONFIG}/bimg_decode64.lib" "${DEST}/"
cp "build/cmake/bx/${CONFIG}/bx64.lib"            "${DEST}/"

echo ""
echo "Installed ${CONFIG} bgfx static libs into ${DEST}"
echo "Now link the engine in the matching config, e.g.:"
echo "  msbuild .build/vsproject/vpx.vcxproj /p:Configuration=Release_BGFX /p:Platform=x64"

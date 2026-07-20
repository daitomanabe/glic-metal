cmake_minimum_required(VERSION 3.16)
project(glic_metal VERSION 1.0.0 LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_EXPORT_COMPILE_COMMANDS ON)

# stb library is included as a git submodule in external/stb

# Core source files
set(CORE_SOURCES
    src/glic.cpp
    src/planes.cpp
    src/colorspaces.cpp
    src/segment.cpp
    src/prediction.cpp
    src/quantization.cpp
    src/wavelet.cpp
    src/encoding.cpp
    src/bitio.cpp
    src/effects.cpp
    src/preset_loader.cpp
    src/realtime.cpp
    src/realtime_certification.cpp
    src/realtime_cpu.cpp
    src/original_realtime.cpp
)

# Header files
set(HEADERS
    src/glic.hpp
    src/planes.hpp
    src/colorspaces.hpp
    src/segment.hpp
    src/prediction.hpp
    src/quantization.hpp
    src/wavelet.hpp
    src/encoding.hpp
    src/bitio.hpp
    src/config.hpp
    src/effects.hpp
    src/preset_loader.hpp
    src/realtime.hpp
    src/realtime_certification.hpp
    src/realtime_internal.hpp
    src/realtime_cpu.hpp
    src/realtime_metal.hpp
    src/original_realtime.hpp
)

add_library(glic_core STATIC ${CORE_SOURCES} ${HEADERS})

target_include_directories(glic_core
    PUBLIC ${CMAKE_SOURCE_DIR}/src
    PRIVATE
    ${CMAKE_SOURCE_DIR}/src
    ${CMAKE_SOURCE_DIR}/external/stb
)

if(APPLE)
    enable_language(OBJCXX)

    set(GLIC_XCODE_DEVELOPER_DIR "/Applications/Xcode.app/Contents/Developer"
        CACHE PATH "Xcode developer directory used to compile Metal shaders")
    if(NOT EXISTS "${GLIC_XCODE_DEVELOPER_DIR}/usr/bin/xcodebuild")
        message(FATAL_ERROR "Full Xcode is required for the Metal backend: ${GLIC_XCODE_DEVELOPER_DIR}")
    endif()

    set(GLIC_METAL_AIR "${CMAKE_BINARY_DIR}/glic_realtime.air")
    set(GLIC_METALLIB "${CMAKE_BINARY_DIR}/glic_realtime.metallib")
    add_custom_command(
        OUTPUT "${GLIC_METAL_AIR}"
        COMMAND ${CMAKE_COMMAND} -E env
            "DEVELOPER_DIR=${GLIC_XCODE_DEVELOPER_DIR}"
            /usr/bin/xcrun -sdk macosx metal -std=metal3.1
            -c "${CMAKE_SOURCE_DIR}/src/glic_realtime.metal"
            -o "${GLIC_METAL_AIR}"
        DEPENDS "${CMAKE_SOURCE_DIR}/src/glic_realtime.metal"
        VERBATIM
    )
    add_custom_command(
        OUTPUT "${GLIC_METALLIB}"
        COMMAND ${CMAKE_COMMAND} -E env
            "DEVELOPER_DIR=${GLIC_XCODE_DEVELOPER_DIR}"
            /usr/bin/xcrun -sdk macosx metallib
            "${GLIC_METAL_AIR}" -o "${GLIC_METALLIB}"
        DEPENDS "${GLIC_METAL_AIR}"
        VERBATIM
    )
    add_custom_target(glic_metal_shaders DEPENDS "${GLIC_METALLIB}")

    target_sources(glic_core PRIVATE src/realtime_metal.mm)
    set_source_files_properties(src/realtime_metal.mm PROPERTIES COMPILE_OPTIONS "-fobjc-arc")
    add_dependencies(glic_core glic_metal_shaders)
    target_compile_definitions(glic_core PRIVATE
        GLIC_HAS_METAL=1
        "GLIC_METALLIB_PATH=\"${GLIC_METALLIB}\""
    )
    target_link_libraries(glic_core PUBLIC "-framework Foundation" "-framework Metal")
endif()

add_executable(glic src/main.cpp)
target_link_libraries(glic PRIVATE glic_core)

add_executable(glic_realtime_bench benchmarks/realtime_benchmark.cpp)
target_link_libraries(glic_realtime_bench PRIVATE glic_core)

add_executable(glic_original_realtime_bench
               benchmarks/original_realtime_benchmark.cpp)
target_link_libraries(glic_original_realtime_bench PRIVATE glic_core)

add_executable(glic_realtime_filter tools/realtime_filter.cpp)
target_link_libraries(glic_realtime_filter PRIVATE glic_core)

add_executable(glic_original_visual_filter tools/original_visual_filter.cpp)
target_link_libraries(glic_original_visual_filter PRIVATE glic_core)

add_executable(glic_realtime_search tools/realtime_search.cpp)
target_link_libraries(glic_realtime_search PRIVATE glic_core)

add_executable(glic_realtime_certify tools/realtime_certify.cpp)
target_link_libraries(glic_realtime_certify PRIVATE glic_core)

# Compiler warnings
if(MSVC)
    target_compile_options(glic_core PRIVATE /W4)
    target_compile_options(glic_realtime_search PRIVATE /W4)
    target_compile_options(glic_realtime_certify PRIVATE /W4)
else()
    target_compile_options(glic_core PRIVATE -Wall -Wextra -O3)
    target_compile_options(glic_realtime_search PRIVATE -Wall -Wextra -Wpedantic)
    target_compile_options(glic_realtime_certify PRIVATE -Wall -Wextra -Wpedantic)
endif()

include(CTest)
if(BUILD_TESTING)
    add_test(NAME glic_realtime_certifier_selftest
             COMMAND glic_realtime_certify --selftest)

    find_package(Python3 COMPONENTS Interpreter)
    if(Python3_Interpreter_FOUND)
        add_test(NAME glic_performance_certification_adapter_selftest
                 COMMAND "${Python3_EXECUTABLE}"
                         "${CMAKE_SOURCE_DIR}/scripts/certify_search_performance.py"
                         --selftest)
        add_test(NAME glic_search_ranking
                 COMMAND "${Python3_EXECUTABLE}" "${CMAKE_SOURCE_DIR}/tests/test_search_ranking.py")
        add_test(NAME glic_original_visual_stream_filter
                 COMMAND "${Python3_EXECUTABLE}"
                         "${CMAKE_SOURCE_DIR}/tests/test_original_visual_filter.py"
                         --filter "$<TARGET_FILE:glic_original_visual_filter>"
                         --presets "${CMAKE_SOURCE_DIR}/presets")
        add_test(NAME glic_original_preset_catalog
                 COMMAND "${Python3_EXECUTABLE}"
                         "${CMAKE_SOURCE_DIR}/tests/test_original_preset_catalog.py")
        add_test(NAME glic_original_visual_tier_analysis
                 COMMAND "${Python3_EXECUTABLE}"
                         "${CMAKE_SOURCE_DIR}/tests/test_analyze_original_visual_tier.py")
        add_test(NAME glic_process_video_helpers
                 COMMAND "${Python3_EXECUTABLE}"
                         "${CMAKE_SOURCE_DIR}/tests/test_process_video_helpers.py")
    endif()

    add_executable(glic_realtime_tests tests/realtime_tests.cpp)
    target_link_libraries(glic_realtime_tests PRIVATE glic_core)
    target_compile_definitions(glic_realtime_tests PRIVATE
        "GLIC_TEST_PRESETS_DIR=\"${CMAKE_SOURCE_DIR}/presets\""
    )
    add_test(NAME glic_realtime_all_presets COMMAND glic_realtime_tests)

    add_executable(glic_preset_loader_tests tests/preset_loader_tests.cpp)
    target_link_libraries(glic_preset_loader_tests PRIVATE glic_core)
    target_compile_definitions(glic_preset_loader_tests PRIVATE
        "GLIC_TEST_PRESETS_DIR=\"${CMAKE_SOURCE_DIR}/presets\""
    )
    add_test(NAME glic_preset_loader_golden COMMAND glic_preset_loader_tests)

    add_executable(glic_original_realtime_tests
                   tests/original_realtime_tests.cpp)
    target_link_libraries(glic_original_realtime_tests PRIVATE glic_core)
    target_compile_definitions(glic_original_realtime_tests PRIVATE
        "GLIC_TEST_PRESETS_DIR=\"${CMAKE_SOURCE_DIR}/presets\""
    )
    add_test(NAME glic_original_realtime_cpu_reference
             COMMAND glic_original_realtime_tests)

    if(APPLE)
        add_executable(glic_realtime_metal_interop_tests tests/realtime_metal_interop_tests.mm)
        set_source_files_properties(tests/realtime_metal_interop_tests.mm PROPERTIES COMPILE_OPTIONS "-fobjc-arc")
        target_link_libraries(glic_realtime_metal_interop_tests PRIVATE glic_core)
        target_compile_definitions(glic_realtime_metal_interop_tests PRIVATE
            "GLIC_TEST_PRESETS_DIR=\"${CMAKE_SOURCE_DIR}/presets\""
        )
        add_test(NAME glic_realtime_metal_texture_interop COMMAND glic_realtime_metal_interop_tests)
    endif()
endif()

# Installation
install(TARGETS glic glic_realtime_bench glic_realtime_filter
                glic_original_visual_filter glic_original_realtime_bench
                glic_realtime_search
                glic_realtime_certify
        RUNTIME DESTINATION bin)
install(PROGRAMS scripts/process_video.py
        DESTINATION bin
        RENAME glic_process_video)
install(PROGRAMS scripts/run_search_supervisor.sh scripts/build_search_catalog.py
        scripts/analyze_search_images.py scripts/perceptual_image_features.py
        scripts/rank_search_results.py scripts/build_ranked_catalog.sh
        scripts/run_ranking_watcher.sh scripts/certify_search_performance.py
        scripts/build_original_preset_catalog.py
        scripts/analyze_original_visual_tier.py
        DESTINATION bin)
install(DIRECTORY presets/
        DESTINATION share/glic-metal/presets)
install(FILES presets.upstream.sha256
        DESTINATION share/glic-metal)
if(APPLE)
    install(FILES "${GLIC_METALLIB}" DESTINATION lib/glic)
endif()

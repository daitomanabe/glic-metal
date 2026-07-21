cmake_minimum_required(VERSION 3.16)
project(glic_metal VERSION 1.0.0 LANGUAGES C CXX)

include(GNUInstallDirs)
include(CMakePackageConfigHelpers)

if(CMAKE_SOURCE_DIR STREQUAL PROJECT_SOURCE_DIR)
    set(GLIC_TOP_LEVEL_DEFAULT ON)
else()
    set(GLIC_TOP_LEVEL_DEFAULT OFF)
endif()
option(GLIC_BUILD_STANDALONE
       "Build GLIC command-line tools, benchmarks, tests, and macOS app"
       ${GLIC_TOP_LEVEL_DEFAULT})
option(GLIC_INSTALL "Install GLIC Metal and its CMake package"
       ${GLIC_TOP_LEVEL_DEFAULT})

set(GLIC_METAL_PRESETS_DIR "${PROJECT_SOURCE_DIR}/presets" CACHE PATH
    "Runtime directory containing GLIC preset files")
include(cmake/GlicMetalResources.cmake)

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
    src/glic_metal_c.cpp
    src/preset_loader.cpp
    src/realtime.cpp
    src/realtime_certification.cpp
    src/realtime_cpu.cpp
    src/original_realtime.cpp
    src/original_realtime_metal_stub.cpp
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
    src/original_realtime_metal.hpp
    src/processing_math.hpp
    src/processing_random.hpp
)

add_library(glic_core STATIC ${CORE_SOURCES} ${HEADERS})
add_library(GlicMetal::GlicMetal ALIAS glic_core)
set_target_properties(glic_core PROPERTIES
    EXPORT_NAME GlicMetal
    OUTPUT_NAME glic_metal
    POSITION_INDEPENDENT_CODE ON)
target_compile_definitions(glic_core PRIVATE
    "GLIC_METAL_VERSION_STRING=\"${PROJECT_VERSION}\"")

# Processing/JWave evaluates source-level multiply and add operations
# separately. Prevent host compiler contraction in the strict CPU oracle so an
# ARM FMA cannot move an adaptive split or a CDF97 rounding boundary.
if(CMAKE_CXX_COMPILER_ID MATCHES "Clang|GNU")
    set_source_files_properties(src/original_realtime.cpp
        PROPERTIES COMPILE_OPTIONS "-ffp-contract=off")
endif()

target_include_directories(glic_core
    PUBLIC
    $<BUILD_INTERFACE:${PROJECT_SOURCE_DIR}/include>
    $<BUILD_INTERFACE:${PROJECT_SOURCE_DIR}/src>
    $<INSTALL_INTERFACE:${CMAKE_INSTALL_INCLUDEDIR}>
    PRIVATE
    ${PROJECT_SOURCE_DIR}/src
    ${PROJECT_SOURCE_DIR}/external/stb
)

find_package(Threads REQUIRED)
target_link_libraries(glic_core PUBLIC Threads::Threads)

if(APPLE)
    enable_language(OBJCXX)

    set(GLIC_XCODE_DEVELOPER_DIR "/Applications/Xcode.app/Contents/Developer"
        CACHE PATH "Xcode developer directory used to compile Metal shaders")
    if(NOT EXISTS "${GLIC_XCODE_DEVELOPER_DIR}/usr/bin/xcodebuild")
        message(FATAL_ERROR "Full Xcode is required for the Metal backend: ${GLIC_XCODE_DEVELOPER_DIR}")
    endif()

    set(GLIC_METAL_AIR "${PROJECT_BINARY_DIR}/glic_realtime.air")
    set(GLIC_ORIGINAL_METAL_AIR
        "${PROJECT_BINARY_DIR}/glic_original_realtime.air")
    set(GLIC_METALLIB "${PROJECT_BINARY_DIR}/glic_realtime.metallib")
    set(GLIC_METAL_METALLIB "${GLIC_METALLIB}" CACHE FILEPATH
        "Compiled GLIC Metal shader library")
    add_custom_command(
        OUTPUT "${GLIC_METAL_AIR}"
        COMMAND ${CMAKE_COMMAND} -E env
            "DEVELOPER_DIR=${GLIC_XCODE_DEVELOPER_DIR}"
            /usr/bin/xcrun -sdk macosx metal -std=metal3.1
            -c "${PROJECT_SOURCE_DIR}/src/glic_realtime.metal"
            -o "${GLIC_METAL_AIR}"
        DEPENDS "${PROJECT_SOURCE_DIR}/src/glic_realtime.metal"
        VERBATIM
    )
    add_custom_command(
        OUTPUT "${GLIC_ORIGINAL_METAL_AIR}"
        COMMAND ${CMAKE_COMMAND} -E env
            "DEVELOPER_DIR=${GLIC_XCODE_DEVELOPER_DIR}"
            /usr/bin/xcrun -sdk macosx metal -std=metal3.1
            -fmetal-math-mode=safe
            -fmetal-math-fp32-functions=precise
            -c "${PROJECT_SOURCE_DIR}/src/original_realtime_metal_kernel.metal"
            -o "${GLIC_ORIGINAL_METAL_AIR}"
        DEPENDS "${PROJECT_SOURCE_DIR}/src/original_realtime_metal_kernel.metal"
        VERBATIM
    )
    add_custom_command(
        OUTPUT "${GLIC_METALLIB}"
        COMMAND ${CMAKE_COMMAND} -E env
            "DEVELOPER_DIR=${GLIC_XCODE_DEVELOPER_DIR}"
            /usr/bin/xcrun -sdk macosx metallib
            "${GLIC_METAL_AIR}" "${GLIC_ORIGINAL_METAL_AIR}"
            -o "${GLIC_METALLIB}"
        DEPENDS "${GLIC_METAL_AIR}" "${GLIC_ORIGINAL_METAL_AIR}"
        VERBATIM
    )
    add_custom_target(glic_metal_shaders DEPENDS "${GLIC_METALLIB}")

    target_sources(glic_core PRIVATE src/realtime_metal.mm
                                     src/original_realtime_metal.mm)
    set_source_files_properties(src/realtime_metal.mm PROPERTIES COMPILE_OPTIONS "-fobjc-arc")
    set_source_files_properties(src/original_realtime_metal.mm
        PROPERTIES COMPILE_OPTIONS "-fobjc-arc;-ffp-contract=off")
    add_dependencies(glic_core glic_metal_shaders)
    target_compile_definitions(glic_core PRIVATE
        GLIC_HAS_METAL=1
        "GLIC_METALLIB_PATH=\"${GLIC_METALLIB}\""
    )
    target_link_libraries(glic_core PUBLIC "-framework Foundation" "-framework Metal")

    if(GLIC_BUILD_STANDALONE)
      add_executable(glic_webcam_preview MACOSX_BUNDLE
                     apps/glic_webcam_preview.mm)
    set_source_files_properties(apps/glic_webcam_preview.mm
        PROPERTIES COMPILE_OPTIONS "-fobjc-arc;-ffp-contract=off")
    target_compile_definitions(glic_webcam_preview PRIVATE
        "GLIC_SOURCE_PRESETS_DIR=\"${PROJECT_SOURCE_DIR}/presets\"")
    target_link_libraries(glic_webcam_preview PRIVATE
        glic_core
        "-framework Accelerate"
        "-framework AppKit"
        "-framework AVFoundation"
        "-framework CoreMedia"
        "-framework CoreVideo"
        "-framework QuartzCore")
    set_target_properties(glic_webcam_preview PROPERTIES
        OUTPUT_NAME "GLIC Webcam Preview"
        MACOSX_BUNDLE_BUNDLE_NAME "GLIC Webcam Preview"
        MACOSX_BUNDLE_GUI_IDENTIFIER "ws.daito.glic-metal.webcam-preview"
        MACOSX_BUNDLE_BUNDLE_VERSION "1"
        MACOSX_BUNDLE_SHORT_VERSION_STRING "1.0.0"
        MACOSX_BUNDLE_INFO_PLIST
            "${PROJECT_SOURCE_DIR}/resources/GLICWebcamPreview-Info.plist")
    set_property(TARGET glic_webcam_preview APPEND PROPERTY LINK_DEPENDS
        "${PROJECT_SOURCE_DIR}/config/fast-match-allowlist.json"
        "${PROJECT_SOURCE_DIR}/LICENSE"
        "${PROJECT_SOURCE_DIR}/THIRD_PARTY_NOTICES.md")
    add_custom_command(TARGET glic_webcam_preview POST_BUILD
        COMMAND ${CMAKE_COMMAND} -E make_directory
            "$<TARGET_BUNDLE_CONTENT_DIR:glic_webcam_preview>/Resources"
        COMMAND ${CMAKE_COMMAND} -E copy_if_different
            "${GLIC_METALLIB}"
            "$<TARGET_BUNDLE_CONTENT_DIR:glic_webcam_preview>/Resources/glic_realtime.metallib"
        COMMAND ${CMAKE_COMMAND} -E copy_directory
            "${PROJECT_SOURCE_DIR}/presets"
            "$<TARGET_BUNDLE_CONTENT_DIR:glic_webcam_preview>/Resources/Presets"
        COMMAND ${CMAKE_COMMAND} -E copy_if_different
            "${PROJECT_SOURCE_DIR}/config/fast-match-allowlist.json"
            "$<TARGET_BUNDLE_CONTENT_DIR:glic_webcam_preview>/Resources/fast-match-allowlist.json"
        COMMAND ${CMAKE_COMMAND} -E copy_if_different
            "${PROJECT_SOURCE_DIR}/LICENSE"
            "$<TARGET_BUNDLE_CONTENT_DIR:glic_webcam_preview>/Resources/LICENSE"
        COMMAND ${CMAKE_COMMAND} -E copy_if_different
            "${PROJECT_SOURCE_DIR}/THIRD_PARTY_NOTICES.md"
            "$<TARGET_BUNDLE_CONTENT_DIR:glic_webcam_preview>/Resources/THIRD_PARTY_NOTICES.md"
        COMMAND /usr/bin/codesign --force --sign -
            "$<TARGET_BUNDLE_DIR:glic_webcam_preview>"
        VERBATIM)
    endif()
endif()

if(GLIC_BUILD_STANDALONE)
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

add_executable(glic_embed_example examples/embed_c.c)
set_target_properties(glic_embed_example PROPERTIES LINKER_LANGUAGE CXX)
target_link_libraries(glic_embed_example PRIVATE GlicMetal::GlicMetal)
endif()

# Compiler warnings
if(MSVC)
    target_compile_options(glic_core PRIVATE /W4)
    if(GLIC_BUILD_STANDALONE)
        target_compile_options(glic_realtime_search PRIVATE /W4)
        target_compile_options(glic_realtime_certify PRIVATE /W4)
    endif()
else()
    target_compile_options(glic_core PRIVATE -Wall -Wextra -O3)
    if(GLIC_BUILD_STANDALONE)
        target_compile_options(glic_realtime_search PRIVATE -Wall -Wextra -Wpedantic)
        target_compile_options(glic_realtime_certify PRIVATE -Wall -Wextra -Wpedantic)
    endif()
endif()

if(GLIC_BUILD_STANDALONE)
include(CTest)
if(BUILD_TESTING)
    add_test(NAME glic_realtime_certifier_selftest
             COMMAND glic_realtime_certify --selftest)

    find_package(Python3 COMPONENTS Interpreter)
    if(Python3_Interpreter_FOUND)
        add_test(NAME glic_performance_certification_adapter_selftest
                 COMMAND "${Python3_EXECUTABLE}"
                         "${PROJECT_SOURCE_DIR}/scripts/certify_search_performance.py"
                         --selftest)
        add_test(NAME glic_search_ranking
                 COMMAND "${Python3_EXECUTABLE}" "${PROJECT_SOURCE_DIR}/tests/test_search_ranking.py")
        add_test(NAME glic_original_visual_stream_filter
                 COMMAND "${Python3_EXECUTABLE}"
                         "${PROJECT_SOURCE_DIR}/tests/test_original_visual_filter.py"
                         --filter "$<TARGET_FILE:glic_original_visual_filter>"
                         --presets "${PROJECT_SOURCE_DIR}/presets")
        add_test(NAME glic_original_preset_catalog
                 COMMAND "${Python3_EXECUTABLE}"
                         "${PROJECT_SOURCE_DIR}/tests/test_original_preset_catalog.py")
        add_test(NAME glic_original_visual_tier_analysis
                 COMMAND "${Python3_EXECUTABLE}"
                         "${PROJECT_SOURCE_DIR}/tests/test_analyze_original_visual_tier.py")
        add_test(NAME glic_process_video_helpers
                 COMMAND "${Python3_EXECUTABLE}"
                         "${PROJECT_SOURCE_DIR}/tests/test_process_video_helpers.py")
        add_test(NAME glic_original_metal_reference_analysis
                 COMMAND "${Python3_EXECUTABLE}"
                         "${PROJECT_SOURCE_DIR}/tests/test_compare_original_metal_reference.py")
        add_test(NAME glic_fast_match_allowlist
                 COMMAND "${Python3_EXECUTABLE}"
                         "${PROJECT_SOURCE_DIR}/tests/test_build_fast_match_allowlist.py")
        add_test(NAME glic_novel_moderate_preset_selector
                 COMMAND "${Python3_EXECUTABLE}"
                         "${PROJECT_SOURCE_DIR}/tests/test_select_novel_moderate_presets.py")
        add_test(NAME glic_original_metal_validation_adapter_selftest
                 COMMAND "${Python3_EXECUTABLE}"
                         "${PROJECT_SOURCE_DIR}/scripts/validate_original_metal_run.py"
                         --selftest)
        add_test(NAME glic_original_metal_manifest_dirty_source
                 COMMAND "${Python3_EXECUTABLE}"
                         "${PROJECT_SOURCE_DIR}/tests/test_write_original_metal_manifest.py")
        add_test(NAME glic_public_release_layout
                 COMMAND "${Python3_EXECUTABLE}"
                         "${PROJECT_SOURCE_DIR}/scripts/check_public_release.py"
                         --source "${PROJECT_SOURCE_DIR}")
    endif()

    add_executable(glic_realtime_tests tests/realtime_tests.cpp)
    target_link_libraries(glic_realtime_tests PRIVATE glic_core)
    target_compile_definitions(glic_realtime_tests PRIVATE
        "GLIC_TEST_PRESETS_DIR=\"${PROJECT_SOURCE_DIR}/presets\""
    )
    add_test(NAME glic_realtime_all_presets COMMAND glic_realtime_tests)

    add_executable(glic_preset_loader_tests tests/preset_loader_tests.cpp)
    target_link_libraries(glic_preset_loader_tests PRIVATE glic_core)
    target_compile_definitions(glic_preset_loader_tests PRIVATE
        "GLIC_TEST_PRESETS_DIR=\"${PROJECT_SOURCE_DIR}/presets\""
    )
    add_test(NAME glic_preset_loader_golden COMMAND glic_preset_loader_tests)

    add_executable(glic_original_realtime_tests
                   tests/original_realtime_tests.cpp)
    target_link_libraries(glic_original_realtime_tests PRIVATE glic_core)
    target_compile_definitions(glic_original_realtime_tests PRIVATE
        "GLIC_TEST_PRESETS_DIR=\"${PROJECT_SOURCE_DIR}/presets\""
    )
    if(CMAKE_CXX_COMPILER_ID MATCHES "Clang|GNU")
        target_compile_options(glic_original_realtime_tests PRIVATE
                               -ffp-contract=off)
    endif()
    add_test(NAME glic_original_realtime_cpu_reference
             COMMAND glic_original_realtime_tests)

    add_executable(glic_embed_c_api_tests tests/embed_c_api_tests.c)
    set_target_properties(glic_embed_c_api_tests PROPERTIES LINKER_LANGUAGE CXX)
    target_link_libraries(glic_embed_c_api_tests PRIVATE GlicMetal::GlicMetal)
    target_compile_definitions(glic_embed_c_api_tests PRIVATE
        "GLIC_TEST_PRESETS_DIR=\"${PROJECT_SOURCE_DIR}/presets\"")
    add_test(NAME glic_embed_c_api COMMAND glic_embed_c_api_tests)

    add_test(NAME glic_installed_consumer
             COMMAND "${CMAKE_COMMAND}"
                     "-DGLIC_SOURCE_DIR=${PROJECT_SOURCE_DIR}"
                     "-DGLIC_BINARY_DIR=${PROJECT_BINARY_DIR}"
                     "-DGLIC_GENERATOR=${CMAKE_GENERATOR}"
                     "-DGLIC_BUILD_CONFIG=$<CONFIG>"
                     -P "${PROJECT_SOURCE_DIR}/tests/run_installed_consumer.cmake")

    add_test(NAME glic_subdirectory_consumer
             COMMAND "${CMAKE_COMMAND}"
                     "-DGLIC_SOURCE_DIR=${PROJECT_SOURCE_DIR}"
                     "-DGLIC_BINARY_DIR=${PROJECT_BINARY_DIR}"
                     "-DGLIC_GENERATOR=${CMAKE_GENERATOR}"
                     "-DGLIC_BUILD_CONFIG=$<CONFIG>"
                     -P "${PROJECT_SOURCE_DIR}/tests/run_subdirectory_consumer.cmake")

    if(APPLE)
        add_test(NAME glic_webcam_preview_selftest
                 COMMAND glic_webcam_preview --self-test)

        add_executable(glic_original_realtime_metal_tests
                       tests/original_realtime_metal_tests.mm)
        set_source_files_properties(tests/original_realtime_metal_tests.mm
                                    PROPERTIES COMPILE_OPTIONS "-fobjc-arc")
        target_link_libraries(glic_original_realtime_metal_tests PRIVATE glic_core)
        target_compile_definitions(glic_original_realtime_metal_tests PRIVATE
            "GLIC_TEST_PRESETS_DIR=\"${PROJECT_SOURCE_DIR}/presets\""
        )
        add_test(NAME glic_original_realtime_metal_reference
                 COMMAND glic_original_realtime_metal_tests)

        add_executable(glic_realtime_metal_interop_tests tests/realtime_metal_interop_tests.mm)
        set_source_files_properties(tests/realtime_metal_interop_tests.mm PROPERTIES COMPILE_OPTIONS "-fobjc-arc")
        target_link_libraries(glic_realtime_metal_interop_tests PRIVATE glic_core)
        target_compile_definitions(glic_realtime_metal_interop_tests PRIVATE
            "GLIC_TEST_PRESETS_DIR=\"${PROJECT_SOURCE_DIR}/presets\""
        )
        add_test(NAME glic_realtime_metal_texture_interop COMMAND glic_realtime_metal_interop_tests)

        add_executable(glic_embed_metal_api_tests
                       tests/embed_metal_api_tests.mm)
        set_source_files_properties(tests/embed_metal_api_tests.mm
                                    PROPERTIES COMPILE_OPTIONS "-fobjc-arc")
        target_link_libraries(glic_embed_metal_api_tests PRIVATE glic_core)
        target_compile_definitions(glic_embed_metal_api_tests PRIVATE
            "GLIC_TEST_PRESETS_DIR=\"${PROJECT_SOURCE_DIR}/presets\""
            "GLIC_TEST_METALLIB=\"${GLIC_METALLIB}\"")
        add_test(NAME glic_embed_metal_api COMMAND glic_embed_metal_api_tests)
    endif()
endif()
endif()

# Installation
if(GLIC_INSTALL)
install(TARGETS glic_core
        EXPORT GlicMetalTargets
        ARCHIVE DESTINATION ${CMAKE_INSTALL_LIBDIR}
        LIBRARY DESTINATION ${CMAKE_INSTALL_LIBDIR}
        RUNTIME DESTINATION ${CMAKE_INSTALL_BINDIR}
        INCLUDES DESTINATION ${CMAKE_INSTALL_INCLUDEDIR})
install(DIRECTORY include/
        DESTINATION ${CMAKE_INSTALL_INCLUDEDIR})
install(EXPORT GlicMetalTargets
        FILE GlicMetalTargets.cmake
        NAMESPACE GlicMetal::
        DESTINATION ${CMAKE_INSTALL_LIBDIR}/cmake/GlicMetal)
configure_package_config_file(
        cmake/GlicMetalConfig.cmake.in
        "${PROJECT_BINARY_DIR}/GlicMetalConfig.cmake"
        INSTALL_DESTINATION ${CMAKE_INSTALL_LIBDIR}/cmake/GlicMetal
        PATH_VARS CMAKE_INSTALL_INCLUDEDIR CMAKE_INSTALL_DATADIR
                  CMAKE_INSTALL_LIBDIR)
write_basic_package_version_file(
        "${PROJECT_BINARY_DIR}/GlicMetalConfigVersion.cmake"
        VERSION ${PROJECT_VERSION}
        COMPATIBILITY SameMajorVersion)
install(FILES
        "${PROJECT_BINARY_DIR}/GlicMetalConfig.cmake"
        "${PROJECT_BINARY_DIR}/GlicMetalConfigVersion.cmake"
        cmake/GlicMetalResources.cmake
        DESTINATION ${CMAKE_INSTALL_LIBDIR}/cmake/GlicMetal)

if(GLIC_BUILD_STANDALONE)
install(TARGETS glic glic_realtime_bench glic_realtime_filter
                glic_original_visual_filter glic_original_realtime_bench
                glic_realtime_search
                glic_realtime_certify
        RUNTIME DESTINATION bin)
if(APPLE)
    install(TARGETS glic_webcam_preview BUNDLE DESTINATION .)
endif()
install(PROGRAMS scripts/process_video.py
        DESTINATION bin
        RENAME glic_process_video)
install(PROGRAMS scripts/run_search_supervisor.sh scripts/build_search_catalog.py
        scripts/analyze_search_images.py scripts/perceptual_image_features.py
        scripts/rank_search_results.py scripts/select_novel_moderate_presets.py
        scripts/build_ranked_catalog.sh
        scripts/run_ranking_watcher.sh scripts/certify_search_performance.py
        scripts/build_original_preset_catalog.py
        scripts/analyze_original_visual_tier.py
        scripts/compare_original_metal_reference.py
        scripts/build_fast_match_allowlist.py
        DESTINATION bin)
endif()
install(DIRECTORY presets/
        DESTINATION ${CMAKE_INSTALL_DATADIR}/glic-metal/presets)
install(FILES presets.upstream.sha256
        DESTINATION ${CMAKE_INSTALL_DATADIR}/glic-metal)
install(FILES README.md LICENSE THIRD_PARTY_NOTICES.md
              docs/BUILDING.md docs/EMBEDDING.md
        DESTINATION ${CMAKE_INSTALL_DATADIR}/doc/glic-metal)
if(APPLE)
    install(FILES "${GLIC_METALLIB}" DESTINATION ${CMAKE_INSTALL_LIBDIR}/glic)
endif()
endif()

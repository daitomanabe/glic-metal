if(NOT DEFINED GLIC_SOURCE_DIR OR NOT DEFINED GLIC_BINARY_DIR OR
   NOT DEFINED GLIC_GENERATOR)
  message(FATAL_ERROR "installed consumer test is missing required paths")
endif()

set(install_dir "${GLIC_BINARY_DIR}/consumer-install")
set(consumer_build_dir "${GLIC_BINARY_DIR}/consumer-build")

set(install_command "${CMAKE_COMMAND}" --install "${GLIC_BINARY_DIR}"
                    --prefix "${install_dir}")
if(DEFINED GLIC_BUILD_CONFIG AND NOT GLIC_BUILD_CONFIG STREQUAL "")
  list(APPEND install_command --config "${GLIC_BUILD_CONFIG}")
endif()
execute_process(
  COMMAND ${install_command}
  RESULT_VARIABLE install_status
  OUTPUT_VARIABLE install_output
  ERROR_VARIABLE install_error)
if(NOT install_status EQUAL 0)
  message(FATAL_ERROR "GlicMetal install failed:\n${install_output}\n${install_error}")
endif()

execute_process(
  COMMAND "${CMAKE_COMMAND}"
          -S "${GLIC_SOURCE_DIR}/tests/consumer"
          -B "${consumer_build_dir}"
          -G "${GLIC_GENERATOR}"
          "-DCMAKE_PREFIX_PATH=${install_dir}"
          -DCMAKE_BUILD_TYPE=Release
  RESULT_VARIABLE configure_status
  OUTPUT_VARIABLE configure_output
  ERROR_VARIABLE configure_error)
if(NOT configure_status EQUAL 0)
  message(FATAL_ERROR
          "consumer configure failed:\n${configure_output}\n${configure_error}")
endif()

set(build_command "${CMAKE_COMMAND}" --build "${consumer_build_dir}")
if(DEFINED GLIC_BUILD_CONFIG AND NOT GLIC_BUILD_CONFIG STREQUAL "")
  list(APPEND build_command --config "${GLIC_BUILD_CONFIG}")
endif()
execute_process(
  COMMAND ${build_command}
  RESULT_VARIABLE build_status
  OUTPUT_VARIABLE build_output
  ERROR_VARIABLE build_error)
if(NOT build_status EQUAL 0)
  message(FATAL_ERROR "consumer build failed:\n${build_output}\n${build_error}")
endif()

if(NOT EXISTS "${consumer_build_dir}/Resources/Presets/default")
  message(FATAL_ERROR "installed consumer did not copy preset resources")
endif()
if(NOT EXISTS "${consumer_build_dir}/Resources/selected-presets.json" OR
   NOT EXISTS "${consumer_build_dir}/Resources/integration-manifest.json" OR
   NOT EXISTS "${consumer_build_dir}/Resources/offline-codec-effects.json" OR
   NOT EXISTS "${consumer_build_dir}/Resources/codec-lab-effects.json")
  message(FATAL_ERROR
          "installed consumer did not copy integration metadata")
endif()
foreach(tool IN ITEMS
    process_multicodec_glitch.py
    process_offline_packet_glitch.py
    evaluate_offline_packet_glitches.py
    process_codec_lab.py
    evolutionary_codec_search.py
    evaluate_effect_difference.py)
  if(NOT EXISTS "${install_dir}/bin/${tool}")
    message(FATAL_ERROR "installed codec tool is missing: ${tool}")
  endif()
endforeach()
if(NOT EXISTS "${install_dir}/share/glic-metal/requirements-qa.txt" OR
   NOT EXISTS
     "${install_dir}/share/doc/glic-metal/DOWNSTREAM_QUICKSTART.md")
  message(FATAL_ERROR
          "installed downstream requirements or quickstart is missing")
endif()
find_program(consumer_python NAMES python3 python REQUIRED)
execute_process(
  COMMAND "${consumer_python}"
          "${install_dir}/bin/evolutionary_codec_search.py" --selftest
  RESULT_VARIABLE search_selftest_status
  OUTPUT_VARIABLE search_selftest_output
  ERROR_VARIABLE search_selftest_error)
if(NOT search_selftest_status EQUAL 0)
  message(FATAL_ERROR
          "installed search selftest failed:\n"
          "${search_selftest_output}\n${search_selftest_error}")
endif()
execute_process(
  COMMAND "${consumer_python}"
          "${install_dir}/bin/evaluate_offline_packet_glitches.py" --help
  RESULT_VARIABLE evaluator_help_status
  OUTPUT_VARIABLE evaluator_help_output
  ERROR_VARIABLE evaluator_help_error)
if(NOT evaluator_help_status EQUAL 0)
  message(FATAL_ERROR
          "installed packet evaluator import failed:\n"
          "${evaluator_help_output}\n${evaluator_help_error}")
endif()
if(APPLE AND NOT EXISTS
   "${consumer_build_dir}/Resources/glic_realtime.metallib")
  message(FATAL_ERROR "installed consumer did not copy the Metal library")
endif()

set(consumer_executable "${consumer_build_dir}/glic_metal_consumer")
if(DEFINED GLIC_BUILD_CONFIG AND NOT GLIC_BUILD_CONFIG STREQUAL "" AND
   EXISTS "${consumer_build_dir}/${GLIC_BUILD_CONFIG}/glic_metal_consumer")
  set(consumer_executable
      "${consumer_build_dir}/${GLIC_BUILD_CONFIG}/glic_metal_consumer")
endif()
execute_process(
  COMMAND "${consumer_executable}"
  RESULT_VARIABLE run_status
  OUTPUT_VARIABLE run_output
  ERROR_VARIABLE run_error)
if(NOT run_status EQUAL 0)
  message(FATAL_ERROR "consumer run failed:\n${run_output}\n${run_error}")
endif()
message(STATUS "${run_output}")

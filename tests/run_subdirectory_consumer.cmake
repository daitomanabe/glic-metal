if(NOT DEFINED GLIC_SOURCE_DIR OR NOT DEFINED GLIC_BINARY_DIR OR
   NOT DEFINED GLIC_GENERATOR)
  message(FATAL_ERROR "subdirectory consumer test is missing required paths")
endif()

set(consumer_build_dir "${GLIC_BINARY_DIR}/subdirectory-consumer-build")
execute_process(
  COMMAND "${CMAKE_COMMAND}"
          -S "${GLIC_SOURCE_DIR}/tests/subdirectory_consumer"
          -B "${consumer_build_dir}"
          -G "${GLIC_GENERATOR}"
          "-DGLIC_SOURCE_DIR=${GLIC_SOURCE_DIR}"
          -DCMAKE_BUILD_TYPE=Release
  RESULT_VARIABLE configure_status
  OUTPUT_VARIABLE configure_output
  ERROR_VARIABLE configure_error)
if(NOT configure_status EQUAL 0)
  message(FATAL_ERROR
          "subdirectory configure failed:\n${configure_output}\n${configure_error}")
endif()

set(build_command "${CMAKE_COMMAND}" --build "${consumer_build_dir}"
                  --target glic_subdirectory_consumer)
if(DEFINED GLIC_BUILD_CONFIG AND NOT GLIC_BUILD_CONFIG STREQUAL "")
  list(APPEND build_command --config "${GLIC_BUILD_CONFIG}")
endif()
execute_process(
  COMMAND ${build_command}
  RESULT_VARIABLE build_status
  OUTPUT_VARIABLE build_output
  ERROR_VARIABLE build_error)
if(NOT build_status EQUAL 0)
  message(FATAL_ERROR
          "subdirectory consumer build failed:\n${build_output}\n${build_error}")
endif()

if(NOT EXISTS "${consumer_build_dir}/Resources/Presets/default")
  message(FATAL_ERROR "subdirectory consumer did not copy preset resources")
endif()
if(NOT EXISTS "${consumer_build_dir}/Resources/selected-presets.json" OR
   NOT EXISTS "${consumer_build_dir}/Resources/integration-manifest.json")
  message(FATAL_ERROR
          "subdirectory consumer did not copy integration metadata")
endif()
if(APPLE AND NOT EXISTS
   "${consumer_build_dir}/Resources/glic_realtime.metallib")
  message(FATAL_ERROR "subdirectory consumer did not copy the Metal library")
endif()

set(consumer_executable
    "${consumer_build_dir}/glic_subdirectory_consumer")
if(DEFINED GLIC_BUILD_CONFIG AND NOT GLIC_BUILD_CONFIG STREQUAL "" AND
   EXISTS "${consumer_build_dir}/${GLIC_BUILD_CONFIG}/glic_subdirectory_consumer")
  set(consumer_executable
      "${consumer_build_dir}/${GLIC_BUILD_CONFIG}/glic_subdirectory_consumer")
endif()
execute_process(
  COMMAND "${consumer_executable}"
  RESULT_VARIABLE run_status
  OUTPUT_VARIABLE run_output
  ERROR_VARIABLE run_error)
if(NOT run_status EQUAL 0)
  message(FATAL_ERROR
          "subdirectory consumer run failed:\n${run_output}\n${run_error}")
endif()
message(STATUS "${run_output}")

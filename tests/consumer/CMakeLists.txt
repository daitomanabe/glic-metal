cmake_minimum_required(VERSION 3.16)
project(glic_metal_consumer LANGUAGES C CXX)

find_package(GlicMetal 1 CONFIG REQUIRED)

if(NOT IS_DIRECTORY "${GLIC_METAL_TOOLS_DIR}" OR
   NOT EXISTS "${GLIC_METAL_PYTHON_REQUIREMENTS}")
  message(FATAL_ERROR
          "GlicMetal package did not expose its offline tools or requirements")
endif()

add_executable(glic_metal_consumer main.c)
set_target_properties(glic_metal_consumer PROPERTIES LINKER_LANGUAGE CXX)
target_link_libraries(glic_metal_consumer PRIVATE GlicMetal::GlicMetal)
target_compile_definitions(glic_metal_consumer PRIVATE
    "CONSUMER_PRESETS_DIR=\"${GLIC_METAL_PRESETS_DIR}\"")
glic_metal_copy_resources(
    TARGET glic_metal_consumer
    DESTINATION "$<TARGET_FILE_DIR:glic_metal_consumer>/Resources")

cmake_minimum_required(VERSION 3.16)
project(glic_metal_subdirectory_consumer LANGUAGES C CXX)

if(NOT DEFINED GLIC_SOURCE_DIR)
  message(FATAL_ERROR "GLIC_SOURCE_DIR is required")
endif()

set(GLIC_BUILD_STANDALONE OFF CACHE BOOL "" FORCE)
set(GLIC_INSTALL OFF CACHE BOOL "" FORCE)
add_subdirectory("${GLIC_SOURCE_DIR}" glic-metal)

if(TARGET glic OR TARGET glic_webcam_preview OR TARGET glic_embed_example)
  message(FATAL_ERROR "standalone GLIC targets leaked into the host project")
endif()
if(NOT TARGET GlicMetal::GlicMetal)
  message(FATAL_ERROR "GlicMetal::GlicMetal target was not created")
endif()

add_executable(glic_subdirectory_consumer ../consumer/main.c)
set_target_properties(glic_subdirectory_consumer PROPERTIES LINKER_LANGUAGE CXX)
target_link_libraries(glic_subdirectory_consumer PRIVATE GlicMetal::GlicMetal)
target_compile_definitions(glic_subdirectory_consumer PRIVATE
    "CONSUMER_PRESETS_DIR=\"${GLIC_SOURCE_DIR}/presets\"")
glic_metal_copy_resources(
    TARGET glic_subdirectory_consumer
    DESTINATION "$<TARGET_FILE_DIR:glic_subdirectory_consumer>/Resources")

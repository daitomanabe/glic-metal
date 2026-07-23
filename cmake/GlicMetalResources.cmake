include(CMakeParseArguments)

function(glic_metal_copy_resources)
  set(options)
  set(one_value_args TARGET DESTINATION)
  cmake_parse_arguments(GLIC_RESOURCE "${options}" "${one_value_args}" ""
                        ${ARGN})
  if(NOT GLIC_RESOURCE_TARGET OR NOT TARGET "${GLIC_RESOURCE_TARGET}")
    message(FATAL_ERROR
            "glic_metal_copy_resources requires an existing TARGET")
  endif()
  if(NOT GLIC_RESOURCE_DESTINATION)
    message(FATAL_ERROR
            "glic_metal_copy_resources requires DESTINATION")
  endif()
  if(NOT IS_DIRECTORY "${GLIC_METAL_PRESETS_DIR}")
    message(FATAL_ERROR
            "GLIC Metal preset directory is missing: ${GLIC_METAL_PRESETS_DIR}")
  endif()
  if(NOT EXISTS "${GLIC_METAL_SELECTED_PRESETS_JSON}")
    message(FATAL_ERROR
            "GLIC Metal selected preset bank is missing: ${GLIC_METAL_SELECTED_PRESETS_JSON}")
  endif()
  if(NOT EXISTS "${GLIC_METAL_INTEGRATION_MANIFEST}")
    message(FATAL_ERROR
            "GLIC Metal integration manifest is missing: ${GLIC_METAL_INTEGRATION_MANIFEST}")
  endif()
  if(NOT EXISTS "${GLIC_METAL_OFFLINE_CODEC_EFFECTS_JSON}")
    message(FATAL_ERROR
            "GLIC Metal offline codec catalog is missing: ${GLIC_METAL_OFFLINE_CODEC_EFFECTS_JSON}")
  endif()
  if(NOT EXISTS "${GLIC_METAL_CODEC_LAB_EFFECTS_JSON}")
    message(FATAL_ERROR
            "GLIC Metal codec lab catalog is missing: ${GLIC_METAL_CODEC_LAB_EFFECTS_JSON}")
  endif()

  add_custom_command(TARGET "${GLIC_RESOURCE_TARGET}" POST_BUILD
    COMMAND "${CMAKE_COMMAND}" -E make_directory
            "${GLIC_RESOURCE_DESTINATION}"
    COMMAND "${CMAKE_COMMAND}" -E copy_directory
            "${GLIC_METAL_PRESETS_DIR}"
            "${GLIC_RESOURCE_DESTINATION}/Presets"
    COMMAND "${CMAKE_COMMAND}" -E copy_if_different
            "${GLIC_METAL_SELECTED_PRESETS_JSON}"
            "${GLIC_RESOURCE_DESTINATION}/selected-presets.json"
    COMMAND "${CMAKE_COMMAND}" -E copy_if_different
            "${GLIC_METAL_INTEGRATION_MANIFEST}"
            "${GLIC_RESOURCE_DESTINATION}/integration-manifest.json"
    COMMAND "${CMAKE_COMMAND}" -E copy_if_different
            "${GLIC_METAL_OFFLINE_CODEC_EFFECTS_JSON}"
            "${GLIC_RESOURCE_DESTINATION}/offline-codec-effects.json"
    COMMAND "${CMAKE_COMMAND}" -E copy_if_different
            "${GLIC_METAL_CODEC_LAB_EFFECTS_JSON}"
            "${GLIC_RESOURCE_DESTINATION}/codec-lab-effects.json"
    VERBATIM)

  if(APPLE)
    add_custom_command(TARGET "${GLIC_RESOURCE_TARGET}" POST_BUILD
      COMMAND "${CMAKE_COMMAND}" -E copy_if_different
              "${GLIC_METAL_METALLIB}"
              "${GLIC_RESOURCE_DESTINATION}/glic_realtime.metallib"
      VERBATIM)
  endif()
endfunction()

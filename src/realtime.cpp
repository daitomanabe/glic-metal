#include "realtime.hpp"

#include "realtime_cpu.hpp"
#if defined(GLIC_HAS_METAL)
#include "realtime_metal.hpp"
#endif

#include <algorithm>
#include <cctype>
#include <memory>
#include <string>

namespace glic {

std::unique_ptr<RealtimeBackend>
createRealtimeBackend(RealtimeBackendKind requested, std::string &error) {
  return createRealtimeBackend(requested, RealtimeBackendCreateOptions{},
                               error);
}

std::unique_ptr<RealtimeBackend>
createRealtimeBackend(RealtimeBackendKind requested,
                      const RealtimeBackendCreateOptions &options,
                      std::string &error) {
#if defined(GLIC_HAS_METAL)
  if (requested == RealtimeBackendKind::AUTO ||
      requested == RealtimeBackendKind::METAL) {
    std::string metalError;
    if (auto backend = createMetalRealtimeBackend(options, metalError)) {
      error.clear();
      return backend;
    }
    if (requested == RealtimeBackendKind::METAL) {
      error = metalError;
      return nullptr;
    }
  }
#else
  if (requested == RealtimeBackendKind::METAL) {
    error = "Metal backend is not available in this build";
    return nullptr;
  }
#endif

  error.clear();
  return createCpuRealtimeBackend();
}

RealtimeBackendKind realtimeBackendKindFromName(const std::string &name) {
  std::string normalized = name;
  std::transform(normalized.begin(), normalized.end(), normalized.begin(),
                 [](unsigned char value) {
                   return static_cast<char>(std::tolower(value));
                 });
  if (normalized == "cpu")
    return RealtimeBackendKind::CPU;
  if (normalized == "metal" || normalized == "gpu")
    return RealtimeBackendKind::METAL;
  return RealtimeBackendKind::AUTO;
}

const char *realtimeBackendKindName(RealtimeBackendKind kind) noexcept {
  switch (kind) {
  case RealtimeBackendKind::CPU:
    return "cpu";
  case RealtimeBackendKind::METAL:
    return "metal";
  default:
    return "auto";
  }
}

} // namespace glic

#include "original_realtime_metal.hpp"

#if !defined(__APPLE__)
namespace glic {

std::unique_ptr<OriginalRealtimeMetalLane>
createOriginalRealtimeMetalLane(std::string &error) {
  return createOriginalRealtimeMetalLane(OriginalRealtimeMetalOptions{}, error);
}

std::unique_ptr<OriginalRealtimeMetalLane>
createOriginalRealtimeMetalLane(const OriginalRealtimeMetalOptions &,
                                std::string &error) {
  error = "The original-style Metal lane is only available on Apple platforms";
  return nullptr;
}

} // namespace glic
#endif

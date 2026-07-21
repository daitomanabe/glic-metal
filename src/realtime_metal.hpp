#pragma once

#include "realtime.hpp"

#include <memory>
#include <string>

namespace glic {

std::unique_ptr<RealtimeBackend> createMetalRealtimeBackend(std::string &error);

std::unique_ptr<RealtimeBackend>
createMetalRealtimeBackend(const RealtimeBackendCreateOptions &options,
                           std::string &error);

} // namespace glic

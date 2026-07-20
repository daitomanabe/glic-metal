#pragma once

#include "realtime.hpp"

#include <memory>
#include <string>

namespace glic {

std::unique_ptr<RealtimeBackend> createMetalRealtimeBackend(std::string &error);

} // namespace glic

#pragma once

#include "realtime.hpp"

#include <memory>

namespace glic {

std::unique_ptr<RealtimeBackend> createCpuRealtimeBackend();

} // namespace glic

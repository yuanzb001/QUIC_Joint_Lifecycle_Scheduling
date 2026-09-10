#include "app/scheduler.hpp"

namespace moq::app {

Scheduler::Scheduler(uint32_t num_streams)
    : num_streams_(num_streams == 0 ? 1 : num_streams) {}

// pick_stream: Decides which QUIC stream to dispatch a specific NALU to.
// This is the core logic for implementing priority scheduling.
// (Currently, this is a basic placeholder implementation that uses modulo arithmetic on nalu_id for round-robin scheduling.
// It has not yet been integrated with H.265's `is_important()` classification.)
// (Future objective: Route critical metadata/I-frames via high-priority Stream 0, and non-critical P-frames via others.)
uint32_t Scheduler::pick_stream(uint32_t nalu_id) const {
    return num_streams_ == 0 ? 0 : (nalu_id % num_streams_);
}

} // namespace moq::app
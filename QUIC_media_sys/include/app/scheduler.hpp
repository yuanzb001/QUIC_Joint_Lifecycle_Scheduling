#pragma once
#include <cstdint>
#include <cstddef>

namespace moq::app {

/**
 * @brief Simple round-robin scheduling policy helper class.
 * 
 * Maps a given NALU identifier to a specific target QUIC stream index.
 */
class Scheduler {
public:
    /**
     * @brief Constructor for Scheduler.
     * @param num_streams The total count of available sending streams.
     */
    explicit Scheduler(uint32_t num_streams);

    /**
     * @brief Determines which stream ID to route the given NALU ID through.
     * @param nalu_id The unique sequence index assigned to the source NALU.
     * @return Stream index offset from 0 to num_streams - 1.
     */
    uint32_t pick_stream(uint32_t nalu_id) const;

    /**
     * @brief Gets the total count of configured sending streams.
     */
    uint32_t num_streams() const { return num_streams_; }

private:
    uint32_t num_streams_{1};         ///< Number of available QUIC streams to schedule packets across.
};

} // namespace moq::app
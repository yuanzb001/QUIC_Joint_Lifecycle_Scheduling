#pragma once
#include <cstdint>
#include <map>
#include <vector>
#include <functional>

#include "wire.hpp"

namespace moq::app {

/**
 * @brief Reassembles fragmented packets back into their original contiguous NALU form.
 * 
 * Handles out-of-order packets due to multi-stream QUIC delivery, buffers packets,
 * and outputs contiguous fully assembled NALUs in correct strict sequential order (HOL ordering).
 */
class Reassembler {
public:
    /**
     * @brief Callback function type invoked when a contiguous NALU is successfully reassembled.
     * @param data Pointer to the contiguous NALU bytes.
     * @param len Total length of the NALU bytes.
     */
    using WriteFn = std::function<void(const uint8_t* data, size_t len)>;

    /**
     * @brief Constructor for Reassembler.
     * @param writer Callback sink where fully assembled NALU streams are flushed.
     */
    explicit Reassembler(WriteFn writer);

    /**
     * @brief Accepts a received packet chunk (header + payload) from the transport layer.
     * 
     * Places the fragment into the reassembly buffer slot corresponding to its nalu_id,
     * and triggers checks to flush completed sequential NALUs.
     * 
     * @param hdr Header metadata of the received packet.
     * @param payload Pointer to the payload segment memory.
     * @param len Length of the payload segment in bytes.
     */
    void on_packet(const PacketHeader& hdr,
                   const uint8_t* payload,
                   size_t len);

    /**
     * @brief Retrieve the next expected NALU sequence identifier.
     */
    uint32_t expected_id() const { return expected_id_; }

    /**
     * @brief Get the count of currently buffered (incomplete or out-of-order) NALUs.
     */
    size_t buffered_count() const { return buffer_.size(); }

private:
    /**
     * @brief Checks the buffer and writes out all consecutive completed NALUs starting from expected_id_.
     */
    void flush_contiguous();

    WriteFn writer_;              ///< Reassembled output writer target.
    uint32_t expected_id_{0};     ///< Next strictly expected NALU ID sequence number.

    /**
     * @brief Helper structure containing fragments of a single NALU.
     */
    struct ReassemblyBuffer {
        uint16_t total_fragments = 0;   ///< Total fragments making up the NALU (retrieved from packet headers).
        uint16_t received_count = 0;    ///< Count of unique fragments received so far.
        // Key: fragment_idx, Value: payload bytes
        std::map<uint16_t, std::vector<uint8_t>> fragments; ///< Sorted map storing fragments by their index.

        /**
         * @brief Checks if all fragments for this NALU have arrived.
         */
        bool is_complete() const {
            return total_fragments > 0 && received_count == total_fragments;
        }
    };

    std::map<uint32_t, ReassemblyBuffer> buffer_; ///< Maps NALU ID to its corresponding reassembly storage buffer.
};

} // namespace moq::app
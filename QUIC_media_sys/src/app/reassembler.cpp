#include "app/reassembler.hpp"
#include <cstring>
#include <iostream>

namespace moq::app {

Reassembler::Reassembler(WriteFn writer)
    : writer_(std::move(writer)) {}

// on_packet: Processes a single fragment received from the QUIC transport layer.
// 1. Locate the reassembly buffer slot corresponding to the nalu_id.
// 2. Check if this fragment_idx has already been received (filter out duplicates from QUIC retries).
// 3. If it is new, copy the payload bytes to the correct index in the fragment map.
// 4. Update the fragment counts, noting the expected total fragment count if it's the first fragment of this NALU.
// 5. Attempt to flush any complete, consecutive NALUs to the output writer.
void Reassembler::on_packet(const moq::PacketHeader& hdr,
                            const uint8_t* payload,
                            size_t len) {
    auto& slot = buffer_[hdr.nalu_id];
    
    // Check if we haven't seen this fragment before (to prevent duplicate processing from network retries)
    if (slot.fragments.find(hdr.fragment_idx) == slot.fragments.end()) {
        // Deep copy the payload into the precise map index (acts as a sorting buffer)
        slot.fragments[hdr.fragment_idx].assign(payload, payload + hdr.payload_len);
        slot.received_count++;
        // Update total fragments if this is the first packet for the NALU
        if (slot.total_fragments == 0) {
            slot.total_fragments = hdr.fragment_count;
        }
    }

    // Try to flush contiguous full NALUs
    flush_contiguous();
}

// flush_contiguous: Attempts to flush fully reassembled NALUs in sequential order.
// To ensure the output bitstream is not corrupted, we enforce strict Head-of-Line ordering by NALU ID.
// 1. Check if the next expected NALU (expected_id_) is present in the buffer.
// 2. Check if all fragments for that NALU have been received (slot.is_complete() is true).
// 3. If complete, concatenate all fragments in correct sequence order and write them out via writer_ (e.g., FileSink).
// 4. Remove the NALU buffer slot to free memory, increment expected_id_, and check for the next NALU.
void Reassembler::flush_contiguous() {
    while (true) {
        auto it = buffer_.find(expected_id_);
        if (it == buffer_.end()) break;

        const auto& slot = it->second;
        
        // Strict Head-of-Line ordering: If the NALU is not fully received yet, we cannot advance.
        // This ensures the H.265 decoder NEVER receives partial or corrupted frames out of order.
        if (!slot.is_complete()) {
            break;
        }

        // Write all fragments in sequential order into the application sink (e.g. File)
        for (uint16_t i = 0; i < slot.total_fragments; ++i) {
            const auto& bytes = slot.fragments.at(i);
            if (!bytes.empty()) {
                writer_(bytes.data(), bytes.size());
            }
        }

        // Free memory and advance expected NALU sequence
        buffer_.erase(it);
        expected_id_++;
    }
}

} // namespace moq::app
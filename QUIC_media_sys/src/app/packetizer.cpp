#include "app/packetizer.hpp"
#include <algorithm>
#include <cstring>
#include <cmath>

namespace moq::app {

DefaultPacketizer::DefaultPacketizer(size_t max_payload_size)
    : max_payload_size_(max_payload_size) {}

// packetize: Slices a single large H.265 NALU into multiple small packets (fragments) fitting the payload size limit.
// Although QUIC implements transport-layer packetization, doing it at the application layer provides higher 
// granular control (e.g., tracking the transmission/retransmission status of individual fragments).
// 1. Calculate the total number of fragments needed based on the NALU size and the max payload limit.
// 2. Loop to slice the NALU, extracting a chunk_size segment each time.
// 3. For each sliced fragment, attach a 16-byte PacketHeader containing its index, total fragment count, etc.
// 4. Return the generated packets, ready to be sent over MsQuic streams.
std::vector<Packet> DefaultPacketizer::packetize(const Nalu& nalu, uint32_t nalu_id) {
    if (nalu.size == 0) return {};

    // Calculate total fragments needed based on NALU size and MTU chunk limits
    uint16_t fragment_count = static_cast<uint16_t>(std::ceil(static_cast<double>(nalu.size) / max_payload_size_));
    std::vector<Packet> packets;
    packets.reserve(fragment_count);

    size_t offset = 0;
    uint16_t idx = 0;

    // Slice the large NALU into sequential chunks
    while (offset < nalu.size) {
        // Ensure the last chunk does not exceed remaining data bounds
        size_t chunk_size = std::min(max_payload_size_, nalu.size - offset);

        Packet pkt;
        // Populate standard 16-byte aligned header for the receiver Reassembler
        pkt.hdr.nalu_id = nalu_id;
        pkt.hdr.payload_len = static_cast<uint32_t>(chunk_size);
        pkt.hdr.flags = 0;
        pkt.hdr.reserved = 0;
        pkt.hdr.fragment_idx = idx;              // Current chunk index (0 to N-1)
        pkt.hdr.fragment_count = fragment_count; // Total chunks N

        // Deep copy the exact byte slice from the parsed NALU memory
        pkt.payload.assign(nalu.data + offset, nalu.data + offset + chunk_size);
        packets.push_back(std::move(pkt));

        // Advance to next slice
        offset += chunk_size;
        idx++;
    }

    return packets;
}

// packetize: Slices a single AV1 frame (including its encapsulated IVF headers) into multiple small packets.
// 1. Calculate the total number of fragments needed based on the frame size and the max payload limit.
// 2. Loop to slice the frame, extracting a chunk_size segment each time.
// 3. Attach a 16-byte PacketHeader to each fragment. We reuse the nalu_id field for frame_id.
// 4. Return the generated packets, ready to be sent over MsQuic streams.
std::vector<Packet> DefaultPacketizer::packetize(const Av1Frame& frame, uint32_t frame_id) {
    if (frame.size == 0) return {};

    uint16_t fragment_count = static_cast<uint16_t>(std::ceil(static_cast<double>(frame.size) / max_payload_size_));
    std::vector<Packet> packets;
    packets.reserve(fragment_count);

    size_t offset = 0;
    uint16_t idx = 0;

    while (offset < frame.size) {
        size_t chunk_size = std::min(max_payload_size_, frame.size - offset);

        Packet pkt;
        pkt.hdr.nalu_id = frame_id; // Reuse nalu_id field for frame_id
        pkt.hdr.payload_len = static_cast<uint32_t>(chunk_size);
        pkt.hdr.flags = 0;
        pkt.hdr.reserved = 0;
        pkt.hdr.fragment_idx = idx;
        pkt.hdr.fragment_count = fragment_count;

        pkt.payload.assign(frame.data + offset, frame.data + offset + chunk_size);
        packets.push_back(std::move(pkt));

        offset += chunk_size;
        idx++;
    }

    return packets;
}

} // namespace moq::app
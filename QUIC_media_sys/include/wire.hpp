#pragma once
#include <cstdint>

namespace moq {

/**
 * @brief Fixed-size header for application-layer packets transmitted over QUIC.
 * 
 * This header prepends every packet payload sent over the QUIC transport,
 * enabling the receiver to reassemble fragmented NALUs and track their metrics.
 * 
 * Note: Network byte order (big-endian) is recommended when finalizing the wire format
 * for cross-platform compatibility, though current implementations may use host byte order.
 */
#pragma pack(push, 1)
struct PacketHeader {
    uint32_t nalu_id;         ///< Global strictly increasing identifier of the NALU.
    uint32_t payload_len;     ///< Length of the payload in bytes following this header.
    uint16_t flags;           ///< Reserved flags for future enhancements (e.g., frame priority, stream type).
    uint16_t reserved;        ///< Alignment padding or future use (e.g., file type identifier).
    uint16_t fragment_idx;    ///< 0-based sequence index of the current fragment.
    uint16_t fragment_count;  ///< Total number of fragments that comprise the complete parent NALU.
};
#pragma pack(pop)

// Ensure structure layout matches 16 bytes exactly to prevent alignment mismatch during serialization.
static_assert(sizeof(PacketHeader) == 16, "PacketHeader must be exactly 16 bytes");

} // namespace moq
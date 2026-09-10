#pragma once
#include <cstddef>
#include <cstdint>
#include <functional>
#include <vector>
#include "wire.hpp"
#include "h265_parser.hpp" // Included for Nalu struct
#include "av1_parser.hpp"  // Included for Av1Frame struct

namespace moq::app {

/**
 * @brief Represents an application-layer packet containing a header and payload chunk.
 */
struct Packet {
    moq::PacketHeader hdr{};            ///< Pre-allocated 16-byte PacketHeader.
    std::vector<uint8_t> payload;       ///< Fragmented payload bytes.
};

/**
 * @brief Abstract interface for application-layer packetization.
 * 
 * Defines the contract for breaking down large media elements (NALUs) into
 * smaller chunked packets that fit within maximum transmission unit (MTU) limits.
 */
class IPacketizer {
public:
    virtual ~IPacketizer() = default;

    /**
     * @brief Splits a Nalu into one or more application-layer packets.
     * 
     * @param nalu The Nalu reference containing the source data.
     * @param nalu_id The unique sequence index assigned to the source NALU.
     * @return Vector of Packets holding the fragmented segments.
     */
    virtual std::vector<Packet> packetize(const Nalu& nalu, uint32_t nalu_id) = 0;

    /**
     * @brief Splits an AV1 Frame into one or more application-layer packets.
     * 
     * @param frame The Av1Frame reference containing the source data.
     * @param frame_id The unique sequence index assigned to the source AV1 frame.
     * @return Vector of Packets holding the fragmented segments.
     */
    virtual std::vector<Packet> packetize(const Av1Frame& frame, uint32_t frame_id) = 0;
};

/**
 * @brief Default implementation of IPacketizer.
 * 
 * Segments NALUs strictly based on a fixed maximum payload size limit.
 */
class DefaultPacketizer : public IPacketizer {
public:
    /**
     * @brief Constructor for DefaultPacketizer.
     * @param max_payload_size The maximum size in bytes allowed for a single packet's payload.
     */
    explicit DefaultPacketizer(size_t max_payload_size);

    /**
     * @brief Implements packetization by slicing the input Nalu.
     * 
     * @param nalu The Nalu reference containing the source data.
     * @param nalu_id The unique sequence index assigned to the source NALU.
     * @return Vector of Packets holding the fragmented segments.
     */
    std::vector<Packet> packetize(const Nalu& nalu, uint32_t nalu_id) override;

    /**
     * @brief Implements packetization by slicing the input Av1Frame.
     * 
     * @param frame The Av1Frame reference containing the source data.
     * @param frame_id The unique sequence index assigned to the source AV1 frame.
     * @return Vector of Packets holding the fragmented segments.
     */
    std::vector<Packet> packetize(const Av1Frame& frame, uint32_t frame_id) override;

private:
    size_t max_payload_size_;          ///< Configured maximum payload size per packet.
};

} // namespace moq::app
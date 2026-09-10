// include/moq/transport/quic_transport.hpp
#pragma once
#include <cstddef>
#include <cstdint>
#include <functional>
#include <string>
#include "wire.hpp"

namespace moq::transport {

/**
 * @brief Callback invoked when a complete application-layer packet is received.
 * @param stream_id The ID of the stream this packet arrived on.
 * @param hdr The header of the received packet.
 * @param payload Pointer to the start of the payload bytes.
 * @param len Total length of the payload in bytes.
 */
using PacketRecvCb = std::function<void(uint32_t stream_id, const moq::PacketHeader& hdr, const uint8_t* payload, size_t len)>;

/**
 * @brief Detailed information returned when a transmission completes (ACKed or canceled).
 */
struct AckEvent {
    uint32_t stream_id{0};
    uint32_t frame_id{0};
    uint16_t fragment_idx{0};
    bool canceled{false};
    uint64_t delay_us{0};
};

/**
 * @brief Callback invoked when a stream successfully sends its data (ACKed by receiver).
 * @param event Detailed information about the ACK.
 */
using AckCb = std::function<void(const AckEvent& event)>;

/**
 * @brief Network statistics retrieved from the transport layer.
 */
struct NetworkStats {
    uint32_t rtt_us{0};
    uint32_t min_rtt_us{0};
    uint32_t max_rtt_us{0};
    uint32_t rtt_variance_us{0};
    uint16_t send_path_mtu{0};
    uint32_t cwnd_bytes{0};
    uint64_t bytes_sent{0};
    uint64_t bytes_received{0};
    uint64_t send_total_stream_bytes{0};
    uint64_t recv_total_stream_bytes{0};
    uint64_t packets_sent{0};
    uint64_t packets_lost{0};
    uint64_t send_spurious_lost_packets{0};
    uint32_t send_congestion_count{0};
    
    // Additional Network Statistics
    uint64_t estimated_bandwidth_bps{0};
    uint32_t bytes_in_flight{0};
    uint64_t posted_bytes{0};
    uint64_t ideal_bytes{0};
};

/**
 * @brief Configuration settings required to initialize the QUIC transport connection.
 */
struct QuicConfig {
    std::string host;         ///< Client mode: Target server address/IP. Server mode: Bind address (optional).
    uint16_t port{4433};      ///< Target connection port. Defaults to 4433.
    uint32_t num_streams{0};  ///< Number of bidirectional/unidirectional streams to pre-allocate/allow.
    uint16_t stream_scheduling_scheme{0}; ///< 0: FIFO (default), 1: Round Robin

    // Server TLS (Required for server execution)
    std::string cert_file;    ///< Path to the TLS certificate file (e.g., "server.crt").
    std::string key_file;     ///< Path to the private key file (e.g., "server.key").
    
    // Optional MTU configuration (overrides MsQuic automatic path discovery limits if non-zero)
    uint16_t max_mtu{0};       ///< Custom maximum MTU size configuration.
    uint16_t min_mtu{0};       ///< Custom minimum MTU size configuration.
};

/**
 * @brief Abstract interface defining the QUIC transport capabilities.
 * 
 * Abstracting the transport layer facilitates swapping implementations
 * (e.g., using a simulated ns-3 channel vs. a production-grade MsQuic library).
 */
class IQuicTransport {
public:
    virtual ~IQuicTransport() = default;

    /**
     * @brief Starts the transport layer in server listening mode.
     * @param cfg Connection configuration (port, cert files, stream limits).
     * @return true on success, false if startup fails.
     */
    virtual bool start_server(const QuicConfig& cfg) = 0;

    /**
     * @brief Initiates an outbound connection to a target server.
     * @param cfg Connection configuration (host, port, stream limits).
     * @return true on success, false if client startup fails.
     */
    virtual bool start_client(const QuicConfig& cfg) = 0;

    /**
     * @brief Sends an application-layer packet through a specific stream.
     * @param stream_index Index of the target stream (0 to num_streams - 1).
     * @param hdr Header metadata of the packet being sent.
     * @param payload Pointer to the payload data segment.
     * @param is_fin Boolean flag indicating if this is the final send on the stream.
     * @return true if stream send event was scheduled successfully, false otherwise.
     */
    virtual bool send(uint32_t stream_index, const moq::PacketHeader& hdr, const uint8_t* payload, bool is_fin = false) = 0;

    /**
     * @brief Registers the callback callback for incoming packet events.
     * @param cb Callback function matching PacketRecvCb.
     */
    virtual void set_recv_callback(PacketRecvCb cb) = 0;

    /**
     * @brief Registers the callback for ACK events.
     * @param cb Callback function matching AckCb.
     */
    virtual void set_ack_callback(AckCb cb) = 0;

    /**
     * @brief Blocks and runs the event loop of the transport layer until termination.
     */
    virtual void run() = 0;

    /**
     * @brief Gets current network statistics from the underlying QUIC connection.
     * @return A NetworkStats struct with the latest metrics.
     */
    virtual NetworkStats get_network_stats() const = 0;
};

} // namespace moq::transport
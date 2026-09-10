#pragma once

#include <cstdint>
#include <string>
#include <memory>
#include "transport/msquic_transport.hpp"

// Placeholder structs if they don't exist yet
struct QueueStats {};
struct ObjectState {};
struct PacketState {};
// use moq::transport::NetworkStats

class QuicMediaClient {
public:
    QuicMediaClient() = default;
    
    // Disable copy and move semantics to prevent dangling lambda captures
    QuicMediaClient(const QuicMediaClient&) = delete;
    QuicMediaClient& operator=(const QuicMediaClient&) = delete;
    QuicMediaClient(QuicMediaClient&&) = delete;
    QuicMediaClient& operator=(QuicMediaClient&&) = delete;

    // =====================================================
    // Configuration
    // =====================================================

    bool configure_mtu(
        uint16_t min_mtu,
        uint16_t max_mtu
    );

    bool set_payload_size(
        size_t payload_size
    );

    bool set_stream_scheduling_scheme(
        uint16_t scheme
    );


    // =====================================================
    // Connection
    // =====================================================

    bool connect(
        const std::string& host,
        uint16_t port
    );

    void disconnect(bool force = false);

    // =====================================================
    // Stream lifecycle
    // =====================================================

    uint32_t open_stream();

    bool close_stream(
        uint32_t stream_id
    );

    bool drop_stream(
        uint32_t stream_id
    );

    bool set_stream_priority(
        uint32_t stream_id,
        uint16_t priority
    );


    // =====================================================
    // Media object input
    // =====================================================

    uint64_t enqueue_object(
        uint32_t stream_id,
        uint32_t frame_id,
        uint16_t subpic_id,
        uint16_t object_type,
        const uint8_t* data,
        size_t size,
        uint16_t priority = 0,
        bool is_fin = false
    );


    // =====================================================
    // Object-level queue control
    // =====================================================

    bool cancel_object(
        uint64_t object_id
    );

    bool set_object_priority(
        uint64_t object_id,
        uint16_t priority
    );

    bool move_object_to_stream(
        uint64_t object_id,
        uint32_t stream_id
    );


    // =====================================================
    // Packet-level queue control
    // =====================================================

    bool cancel_packet(
        uint64_t packet_id
    );

    bool set_packet_priority(
        uint64_t packet_id,
        uint16_t priority
    );

    bool move_packet_to_stream(
        uint64_t packet_id,
        uint32_t stream_id
    );


    // =====================================================
    // Queue information
    // =====================================================

    QueueStats get_queue_stats() const;

    ObjectState get_object_state(
        uint64_t object_id
    ) const;

    PacketState get_packet_state(
        uint64_t packet_id
    ) const;


    // =====================================================
    // Network feedback
    // =====================================================

    moq::transport::NetworkStats get_network_stats() const;

    void set_ack_callback(std::function<void(const moq::transport::AckEvent&)> cb);


    // =====================================================
    // Shutdown
    // =====================================================

    void shutdown();

private:
    moq::transport::MsQuicTransport quic;
    moq::transport::QuicConfig cfg;
};
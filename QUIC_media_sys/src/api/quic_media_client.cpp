#include "api/quic_media_client.hpp"
#include <iostream>

bool QuicMediaClient::configure_mtu(uint16_t min_mtu, uint16_t max_mtu) {
    cfg.min_mtu = min_mtu;
    cfg.max_mtu = max_mtu;
    return true;
}

bool QuicMediaClient::set_payload_size(size_t payload_size) {
    // Placeholder for actual payload size handling (e.g., configuring packetizer)
    return true;
}

bool QuicMediaClient::set_stream_scheduling_scheme(uint16_t scheme) {
    cfg.stream_scheduling_scheme = scheme;
    return true;
}

bool QuicMediaClient::connect(const std::string& host, uint16_t port) {
    cfg.host = host;
    cfg.port = port;
    
    if (!quic.start_client(cfg)) {
        return false;
    }
    if (!quic.wait_connected()) {
        return false;
    }
    if (!quic.wait_stream_ready()) {
        return false;
    }
    return true;
}

uint32_t QuicMediaClient::open_stream() {
    int32_t stream_id = quic.open_stream(0); // priority 0
    if (stream_id < 0) {
        std::cerr << "[QuicMediaClient] Failed to open stream\n";
        return 0; // return dummy 0 on failure for now, or you could return uint32_t max
    }
    return static_cast<uint32_t>(stream_id);
}

bool QuicMediaClient::close_stream(uint32_t stream_id) {
    return quic.close_stream(stream_id);
}

bool QuicMediaClient::drop_stream(uint32_t stream_id) {
    return quic.drop_stream(stream_id);
}

bool QuicMediaClient::set_stream_priority(uint32_t stream_id, uint16_t priority) {
    return quic.set_stream_priority(stream_id, priority);
}

void QuicMediaClient::disconnect(bool force) {
    quic.disconnect(force);
    quic.set_ack_callback(nullptr);
}

void QuicMediaClient::set_ack_callback(std::function<void(const moq::transport::AckEvent&)> cb) {
    quic.set_ack_callback(std::move(cb));
}

moq::transport::NetworkStats QuicMediaClient::get_network_stats() const {
    // Forward every field straight from MsQuicTransport; a manual field-by-field
    // copy silently drops any stat not explicitly listed (e.g. estimated_bandwidth_bps,
    // bytes_in_flight, posted_bytes, ideal_bytes).
    return quic.get_network_stats();
}

uint64_t QuicMediaClient::enqueue_object(
    uint32_t stream_id,
    uint32_t frame_id,
    uint16_t subpic_id,
    uint16_t object_type,
    const uint8_t* data,
    size_t size,
    uint16_t priority,
    bool is_fin) 
{
    // Convert object parameters to PacketHeader
    moq::PacketHeader hdr{};
    hdr.nalu_id = frame_id; // Reuse nalu_id field for frame_id
    hdr.fragment_idx = 0;
    hdr.fragment_count = 1;
    hdr.payload_len = static_cast<uint32_t>(size);
    // Ignore subpic_id and object_type for now unless moq::PacketHeader is updated

    if (quic.send(stream_id, hdr, data, is_fin)) {
        return frame_id; // return a pseudo object_id
    }
    return static_cast<uint64_t>(-1); // -1 means failed
}

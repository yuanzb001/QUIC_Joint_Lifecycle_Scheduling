#pragma once

#include <cstdint>
#include <string>
#include <memory>
#include <functional>
#include "transport/msquic_transport.hpp"

class QuicMediaServer {
public:
    QuicMediaServer();
    ~QuicMediaServer();

    // Disable copy and move semantics to prevent dangling lambda captures
    QuicMediaServer(const QuicMediaServer&) = delete;
    QuicMediaServer& operator=(const QuicMediaServer&) = delete;
    QuicMediaServer(QuicMediaServer&&) = delete;
    QuicMediaServer& operator=(QuicMediaServer&&) = delete;

    // =====================================================
    // Configuration
    // =====================================================

    bool configure_mtu(
        uint16_t min_mtu,
        uint16_t max_mtu
    );

    // =====================================================
    // Connection
    // =====================================================

    bool start_server(
        const std::string& host,
        uint16_t port,
        const std::string& cert_file,
        const std::string& key_file
    );

    void stop_server();

    void run();

    // =====================================================
    // Callbacks
    // =====================================================

    using RecvCallback = std::function<void(uint32_t stream_id, const uint8_t* data, size_t len)>;
    
    void set_recv_callback(RecvCallback cb);

private:
    moq::transport::MsQuicTransport quic;
    moq::transport::QuicConfig cfg;
    RecvCallback recv_cb_;
    std::mutex recv_cb_mtx_;
};

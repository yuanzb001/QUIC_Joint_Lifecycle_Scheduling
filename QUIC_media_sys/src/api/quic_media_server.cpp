#include "api/quic_media_server.hpp"
#include <iostream>

QuicMediaServer::QuicMediaServer() {
}

QuicMediaServer::~QuicMediaServer() {
    stop_server();
}

bool QuicMediaServer::configure_mtu(uint16_t min_mtu, uint16_t max_mtu) {
    cfg.min_mtu = min_mtu;
    cfg.max_mtu = max_mtu;
    return true;
}

bool QuicMediaServer::start_server(
    const std::string& host,
    uint16_t port,
    const std::string& cert_file,
    const std::string& key_file)
{
    cfg.host = host;
    cfg.port = port;
    cfg.cert_file = cert_file;
    cfg.key_file = key_file;
    
    // Register the internal receive callback
    quic.set_recv_callback([this](uint32_t stream_id, const moq::PacketHeader& hdr, const uint8_t* payload, size_t len) {
        std::lock_guard<std::mutex> lk(recv_cb_mtx_);
        if (recv_cb_) {
            recv_cb_(stream_id, payload, len);
        }
    });

    if (!quic.start_server(cfg)) {
        return false;
    }
    return true;
}

void QuicMediaServer::run() {
    quic.run();
}

void QuicMediaServer::stop_server() {
    quic.stop_server(true); // force disconnect
    
    // Clear the callback while the GIL is definitely held by the user calling stop_server,
    // avoiding Python GC teardown crashes when destroying py::function later.
    std::lock_guard<std::mutex> lk(recv_cb_mtx_);
    recv_cb_ = nullptr;
}

void QuicMediaServer::set_recv_callback(RecvCallback cb) {
    std::lock_guard<std::mutex> lk(recv_cb_mtx_);
    recv_cb_ = std::move(cb);
}

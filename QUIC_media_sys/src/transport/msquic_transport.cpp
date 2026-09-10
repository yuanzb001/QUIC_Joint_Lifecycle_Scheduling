#pragma message("MSQUIC HEADER PATH CHECK")
#include <msquic.h>

// --- Log Toggles ---
//#define ENABLE_PARSE_LOG
//#define ENABLE_RECV_ORDER_LOG
//#define ENABLE_STREAM_EVENT_LOG
//#define ENABLE_ACK_ORDER_LOG
//#define ENABLE_SEND_LOG
//#define ENABLE_SEND_ORDER_LOG

// src/transport/msquic_transport.cpp
#define QUIC_API_ENABLE_PREVIEW_FEATURES 1
#include "transport/msquic_transport.hpp"

#include <cstring>
#include <iostream>
#include <thread>
#include <vector>
#include <chrono>
#include <pthread.h>
#include <unistd.h>

namespace moq::transport {

// A single ALPN for MoQ prototype
static constexpr const char* kAlpn = "moq";
static constexpr uint16_t kDefaultIdleTimeoutMs = 30000;

// ------------------- Helpers -------------------

// NOTE: If you don't have these in your header, keep them there.
// struct SendContext {
//     QUIC_BUFFER qb{};
//     std::vector<uint8_t> storage;
// };

static const char* ConnEventName(QUIC_CONNECTION_EVENT_TYPE t) {
    switch (t) {
    case QUIC_CONNECTION_EVENT_CONNECTED: return "CONNECTED";
    case QUIC_CONNECTION_EVENT_SHUTDOWN_INITIATED_BY_TRANSPORT: return "SHUTDOWN_BY_TRANSPORT";
    case QUIC_CONNECTION_EVENT_SHUTDOWN_INITIATED_BY_PEER: return "SHUTDOWN_BY_PEER";
    case QUIC_CONNECTION_EVENT_SHUTDOWN_COMPLETE: return "SHUTDOWN_COMPLETE";
    case QUIC_CONNECTION_EVENT_PEER_STREAM_STARTED: return "PEER_STREAM_STARTED";
    default: return "OTHER";
    }
}

static const char* StreamEventName(QUIC_STREAM_EVENT_TYPE t) {
    switch (t) {
    case QUIC_STREAM_EVENT_START_COMPLETE: return "START_COMPLETE";
    case QUIC_STREAM_EVENT_RECEIVE: return "RECEIVE";
    case QUIC_STREAM_EVENT_SEND_COMPLETE: return "SEND_COMPLETE";
    case QUIC_STREAM_EVENT_PEER_SEND_SHUTDOWN: return "PEER_SEND_SHUTDOWN";
    case QUIC_STREAM_EVENT_PEER_SEND_ABORTED: return "PEER_SEND_ABORTED";
    case QUIC_STREAM_EVENT_SHUTDOWN_COMPLETE: return "SHUTDOWN_COMPLETE";
    default: return "OTHER";
    }
}

// ------------------- Lifecycle -------------------

// MsQuicTransport Constructor
// Responsible for initializing the MsQuic API table (via MsQuicOpen2) and creating a Registration context.
MsQuicTransport::MsQuicTransport() {
    if (MsQuicOpen2(&api_) != QUIC_STATUS_SUCCESS) {
        std::cerr << "[msquic] MsQuicOpen2 failed\n";
        api_ = nullptr;
        return;
    }

    // Registration
    QUIC_REGISTRATION_CONFIG reg_cfg{};
    reg_cfg.AppName = "QUIC_media_sys";
    reg_cfg.ExecutionProfile = QUIC_EXECUTION_PROFILE_LOW_LATENCY;

    QUIC_STATUS rst = api_->RegistrationOpen(&reg_cfg, &registration_);
    if (rst != QUIC_STATUS_SUCCESS) {
        std::cerr << "[msquic] RegistrationOpen failed, status=0x"
                  << std::hex << rst << std::dec << "\n";
        registration_ = nullptr;
        return;
    }
}

MsQuicTransport::~MsQuicTransport() {
    // Stop run loop
    running_.store(false);
    connected_.store(false);

    {
        std::lock_guard<std::mutex> lk(mtx_);
        cv_.notify_all();
        cv_connected_.notify_all();
    }

    running_.store(false);
    
    if (is_client_ && connection_) {
        api_->ConnectionClose(connection_);
    } else if (!is_client_) {
        std::unordered_set<HQUIC> conns_to_close;
        {
            std::lock_guard<std::mutex> lk(mtx_);
            conns_to_close = client_connections_;
            client_connections_.clear();
        }
        for (HQUIC conn : conns_to_close) {
            api_->ConnectionClose(conn);
        }
    }
    
    if (listener_) {
        api_->ListenerClose(listener_);
        listener_ = nullptr;
    }
    if (configuration_) {
        api_->ConfigurationClose(configuration_);
        configuration_ = nullptr;
    }
    if (registration_) {
        api_->RegistrationClose(registration_);
        registration_ = nullptr;
    }
    if (api_) {
        MsQuicClose(api_);
        api_ = nullptr;
    }
}

// ------------------- Wait helpers -------------------

bool MsQuicTransport::wait_connected() {
    // Wait until connected_ becomes true or running_ becomes false
    std::unique_lock<std::mutex> lk(mtx_);
    cv_connected_.wait(lk, [&] { return connected_.load() || !running_.load(); });
    return connected_.load();
}

bool MsQuicTransport::wait_stream_ready() {
    if (cfg_.num_streams == 0) return true;
    
    std::unique_lock<std::mutex> lk(mtx_stream_ready_);
    return cv_stream_ready_.wait_for(
        lk,
        std::chrono::seconds(5),
        [&]{ return stream_ready_.load(); });
}

void MsQuicTransport::set_recv_callback(PacketRecvCb cb) {
    recv_cb_ = std::move(cb);
}

void MsQuicTransport::set_ack_callback(AckCb cb) {
    ack_cb_ = std::move(cb);
}

// ------------------- Configuration -------------------

bool MsQuicTransport::init_configuration_client() {
    QUIC_BUFFER alpn;
    alpn.Buffer = (uint8_t*)kAlpn;
    alpn.Length = (uint32_t)std::strlen(kAlpn);

    QUIC_SETTINGS settings{};
    settings.IsSet.IdleTimeoutMs = TRUE;
    settings.IdleTimeoutMs = kDefaultIdleTimeoutMs;

    settings.IsSet.CongestionControlAlgorithm = TRUE;
    settings.CongestionControlAlgorithm = QUIC_CONGESTION_CONTROL_ALGORITHM_BBR;

    if (cfg_.max_mtu > 0) {
        settings.IsSet.MaximumMtu = TRUE;
        settings.MaximumMtu = cfg_.max_mtu;
    }
    if (cfg_.min_mtu > 0) {
        settings.IsSet.MinimumMtu = TRUE;
        settings.MinimumMtu = cfg_.min_mtu;
    }

    settings.IsSet.StreamRecvWindowDefault = TRUE;
    settings.StreamRecvWindowDefault = 128 << 20; // 128MB

    settings.IsSet.ConnFlowControlWindow = TRUE;
    settings.ConnFlowControlWindow = 128 << 20; // 128MB

    QUIC_STATUS st = api_->ConfigurationOpen(
        registration_,
        &alpn, 1,
        &settings, sizeof(settings),
        nullptr,
        &configuration_);

    std::cerr << "[msquic] ConfigurationOpen(client) st=0x"
              << std::hex << st << std::dec
              << " conf=" << configuration_ << "\n";

    if (st != QUIC_STATUS_SUCCESS) return false;

    QUIC_CREDENTIAL_CONFIG cred{};
    cred.Type = QUIC_CREDENTIAL_TYPE_NONE;
    cred.Flags = QUIC_CREDENTIAL_FLAG_CLIENT |
                 QUIC_CREDENTIAL_FLAG_NO_CERTIFICATE_VALIDATION;

    st = api_->ConfigurationLoadCredential(configuration_, &cred);

    std::cerr << "[msquic] ConfigurationLoadCredential(client) st=0x"
              << std::hex << st << std::dec << "\n";

    return st == QUIC_STATUS_SUCCESS;
}

bool MsQuicTransport::init_configuration_server() {
    std::cerr << "[msquic] cfg_.cert_file = '" << cfg_.cert_file << "'\n";
    std::cerr << "[msquic] cfg_.key_file  = '" << cfg_.key_file  << "'\n";

    if (access(cfg_.cert_file.c_str(), R_OK) != 0) {
        perror("[msquic] cert access");
    }
    if (access(cfg_.key_file.c_str(), R_OK) != 0) {
        perror("[msquic] key access");
    }

    QUIC_BUFFER alpn;
    alpn.Buffer = (uint8_t*)kAlpn;
    alpn.Length = (uint32_t)std::strlen(kAlpn);

    QUIC_SETTINGS settings{};
    settings.IsSet.IdleTimeoutMs = TRUE;
    settings.IdleTimeoutMs = 300000; // 5 min

    settings.IsSet.CongestionControlAlgorithm = TRUE;
    settings.CongestionControlAlgorithm = QUIC_CONGESTION_CONTROL_ALGORITHM_BBR;

    // Allow multiple peer streams (server side)
    settings.IsSet.PeerBidiStreamCount = TRUE;
    settings.PeerBidiStreamCount = 16;

    settings.IsSet.PeerUnidiStreamCount = TRUE;
    settings.PeerUnidiStreamCount = 16;

    // Flow control windows (tune as needed)
    settings.IsSet.StreamRecvWindowDefault = TRUE;
    settings.StreamRecvWindowDefault = 128 << 20; // 128MB

    settings.IsSet.ConnFlowControlWindow = TRUE;
    settings.ConnFlowControlWindow = 128 << 20; // 128MB

    // Keepalive (0 = disabled in many versions)
    settings.IsSet.KeepAliveIntervalMs = TRUE;
    settings.KeepAliveIntervalMs = 0;

    if (cfg_.max_mtu > 0) {
        settings.IsSet.MaximumMtu = TRUE;
        settings.MaximumMtu = cfg_.max_mtu;
    }
    if (cfg_.min_mtu > 0) {
        settings.IsSet.MinimumMtu = TRUE;
        settings.MinimumMtu = cfg_.min_mtu;
    }

    if (api_->ConfigurationOpen(
            registration_,
            &alpn, 1,
            &settings, sizeof(settings),
            nullptr,
            &configuration_) != QUIC_STATUS_SUCCESS) {
        std::cerr << "[msquic] ConfigurationOpen(server) failed\n";
        return false;
    }

    // TLS cert
    cert_file_.CertificateFile = cfg_.cert_file.c_str();
    cert_file_.PrivateKeyFile  = cfg_.key_file.c_str();

    QUIC_CREDENTIAL_CONFIG cred{};
    cred.Type = QUIC_CREDENTIAL_TYPE_CERTIFICATE_FILE;
    cred.Flags = QUIC_CREDENTIAL_FLAG_NONE;
    cred.CertificateFile = &cert_file_;

    QUIC_STATUS st = api_->ConfigurationLoadCredential(configuration_, &cred);
    if (st != QUIC_STATUS_SUCCESS) {
        std::cerr << "[msquic] ConfigurationLoadCredential(server) failed st=0x"
                  << std::hex << st << std::dec << "\n";
        return false;
    }

    return true;
}

// ------------------- Start Server/Client -------------------

// start_server: Starts the QUIC transport in server mode.
// 1. Configure server settings (idle timeouts, certificates, stream count limits).
// 2. Open a Listener to handle inbound client connections.
// 3. Start listening on the designated port.
bool MsQuicTransport::start_server(const QuicConfig& cfg) {
    is_client_ = false;
    if (!api_ || !registration_) return false;

    cfg_ = cfg;

    if (!init_configuration_server()) return false;

    QUIC_STATUS st = api_->ListenerOpen(registration_, listener_callback, this, &listener_);
    if (QUIC_FAILED(st)) {
        std::cerr << "[msquic] ListenerOpen failed st=0x"
                  << std::hex << st << std::dec << "\n";
        return false;
    }

    QUIC_ADDR addr{};
    QuicAddrSetFamily(&addr, QUIC_ADDRESS_FAMILY_INET);
    QuicAddrSetPort(&addr, cfg_.port);

    QUIC_BUFFER alpn;
    alpn.Buffer = (uint8_t*)kAlpn;
    alpn.Length = (uint32_t)std::strlen(kAlpn);

    st = api_->ListenerStart(listener_, &alpn, 1, &addr);
    if (QUIC_FAILED(st)) {
        std::cerr << "[msquic] ListenerStart failed st=0x"
                  << std::hex << st << std::dec << "\n";
        return false;
    }

    running_.store(true);
    connected_.store(false);

    std::cerr << "[msquic] Server listening on port " << cfg_.port << "\n";
    return true;
}

// start_client: Starts the QUIC transport in client mode.
// 1. Configure client settings (MTU boundaries, bypass certificate verification).
// 2. Open a Connection.
// 3. Start the connection to the specified host and port.
bool MsQuicTransport::start_client(const QuicConfig& cfg) {
    is_client_ = true;
    if (!api_ || !registration_) return false;

    cfg_ = cfg;

    std::cerr << "HOST=" << cfg_.host << "\n";
    std::cerr << "PORT=" << cfg_.port << "\n";
    std::cerr << "[client] cfg_.num_streams = " << cfg_.num_streams << "\n";

    // Read SSLKEYLOGFILE env var for TLS key export
    const char* keylog_env = std::getenv("SSLKEYLOGFILE");
    if (keylog_env) {
        key_log_path_ = keylog_env;
        std::cerr << "[msquic] TLS key log will be written to: " << key_log_path_ << "\n";
    }

    if (!init_configuration_client()) return false;

    QUIC_STATUS st = api_->ConnectionOpen(
        registration_,
        MsQuicTransport::connection_callback,
        this,
        &connection_);

    if (QUIC_FAILED(st)) {
        std::cerr << "[msquic] ConnectionOpen failed st=0x"
                  << std::hex << st << std::dec << "\n";
        return false;
    }

    // Register TLS secrets struct BEFORE ConnectionStart so MsQuic fills it during handshake
    if (!key_log_path_.empty()) {
        std::memset(&tls_secrets_, 0, sizeof(tls_secrets_));
        QUIC_STATUS st_sec = api_->SetParam(
            connection_,
            QUIC_PARAM_CONN_TLS_SECRETS,
            sizeof(tls_secrets_),
            &tls_secrets_);
        if (QUIC_FAILED(st_sec)) {
            std::cerr << "[msquic] SetParam(TLS_SECRETS) failed st=0x"
                      << std::hex << st_sec << std::dec << "\n";
        } else {
            std::cerr << "[msquic] TLS secrets capture registered OK\n";
        }
    }

    // Set Stream Scheduling Scheme
    if (cfg_.stream_scheduling_scheme != 0) {
        QUIC_STREAM_SCHEDULING_SCHEME scheme = (QUIC_STREAM_SCHEDULING_SCHEME)cfg_.stream_scheduling_scheme;
        QUIC_STATUS st_sched = api_->SetParam(
            connection_,
            QUIC_PARAM_CONN_STREAM_SCHEDULING_SCHEME,
            sizeof(scheme),
            &scheme);
        if (QUIC_FAILED(st_sched)) {
            std::cerr << "[msquic] SetParam(SCHEDULING_SCHEME) failed st=0x"
                      << std::hex << st_sched << std::dec << "\n";
        } else {
            std::cerr << "[msquic] Stream scheduling scheme set to " 
                      << (scheme == QUIC_STREAM_SCHEDULING_SCHEME_ROUND_ROBIN ? "ROUND_ROBIN" : std::to_string(scheme)) 
                      << "\n";
        }
    } else {
        std::cerr << "[msquic] Stream scheduling scheme set to FIFO (Default)\n";
    }

    st = api_->ConnectionStart(
        connection_,
        configuration_,
        QUIC_ADDRESS_FAMILY_UNSPEC,
        cfg_.host.c_str(),
        cfg_.port);

    if (QUIC_FAILED(st)) {
        std::cerr << "[msquic] ConnectionStart failed st=0x"
                  << std::hex << st << std::dec << "\n";
        return false;
    }

    std::cerr << "[msquic] ConnectionStart ok (st=0x"
              << std::hex << st << std::dec << ")\n";

    running_.store(true);
    connected_.store(false);
    return true;
}

bool MsQuicTransport::open_and_start_client_streams() {
    streams_.clear();

    for (uint32_t i = 0; i < cfg_.num_streams; ++i) {
        uint32_t sid = next_stream_id_++;
        std::cerr << "[client] opening stream idx=" << sid << "\n";

        HQUIC s = nullptr;
        QUIC_STATUS st = api_->StreamOpen(
            connection_,
            QUIC_STREAM_OPEN_FLAG_NONE, // bidirectional
            MsQuicTransport::stream_callback,
            this,
            &s);

        if (QUIC_FAILED(st)) {
            std::cerr << "[msquic] StreamOpen failed idx=" << i
                      << " st=0x" << std::hex << st << std::dec << "\n";
            return false;
        }

        QUIC_STATUS st2 = api_->StreamStart(s, QUIC_STREAM_START_FLAG_IMMEDIATE);
        std::cerr << "[msquic] StreamStart idx=" << sid
                  << " st=0x" << std::hex << st2 << std::dec << "\n";

        if (QUIC_FAILED(st2)) return false;
        streams_[sid] = s;
    }

    std::cerr << "[msquic] Client streams started: " << cfg_.num_streams << "\n";
    return true;
}

int32_t MsQuicTransport::open_stream(uint16_t priority) {
    if (!connected_.load() || !api_) {
        std::cerr << "[msquic] Cannot open stream, not connected or no API\n";
        return -1;
    }
    
    HQUIC new_stream = nullptr;
    QUIC_STATUS st = api_->StreamOpen(
        connection_,
        QUIC_STREAM_OPEN_FLAG_NONE, // bidirectional
        MsQuicTransport::stream_callback,
        this,
        &new_stream);

    if (QUIC_FAILED(st)) {
        std::cerr << "[msquic] Dynamic StreamOpen failed st=0x" << std::hex << st << std::dec << "\n";
        return -1;
    }
    
    if (priority != 0) {
        api_->SetParam(new_stream, QUIC_PARAM_STREAM_PRIORITY, sizeof(priority), &priority);
    }

    QUIC_STATUS st2 = api_->StreamStart(new_stream, QUIC_STREAM_START_FLAG_IMMEDIATE);
    if (QUIC_FAILED(st2)) {
        std::cerr << "[msquic] Dynamic StreamStart failed st=0x" << std::hex << st2 << std::dec << "\n";
        api_->StreamClose(new_stream);
        return -1;
    }
    
    int32_t stream_idx = -1;
    {
        std::lock_guard<std::mutex> lk(mtx_);
        stream_idx = static_cast<int32_t>(next_stream_id_++);
        streams_[stream_idx] = new_stream;
    }
    
    std::cerr << "[msquic] Dynamic stream opened, index=" << stream_idx << "\n";
    return stream_idx;
}

bool MsQuicTransport::close_stream(uint32_t stream_index) {
    std::lock_guard<std::mutex> lk(mtx_);
    auto it = streams_.find(stream_index);
    if (it == streams_.end() || it->second == nullptr) {
        return false; // Invalid or already closed
    }

    HQUIC stream = it->second;
    // Remove from the active send-capable set so new sends are rejected immediately.
    streams_.erase(it);

    // Track in closing_streams_ so the SHUTDOWN_COMPLETE callback can find the
    // handle and call StreamClose after graceful shutdown finishes.
    // (If we left streams_[i] non-null, concurrent sends could still reach it;
    //  if we clear it but don't track it here, StreamClose is never called → resource leak.)
    closing_streams_.insert(stream);

    // Initiate graceful shutdown — MsQuic will flush all pending sends before
    // shutting down the send direction.  StreamClose must NOT be called here;
    // it must wait for QUIC_STREAM_EVENT_SHUTDOWN_COMPLETE in the callback.
    api_->StreamShutdown(stream, QUIC_STREAM_SHUTDOWN_FLAG_GRACEFUL, 0);

    std::cerr << "[msquic] Dynamic stream gracefully closing, index=" << stream_index << "\n";
    return true;
}

bool MsQuicTransport::drop_stream(uint32_t stream_index) {
    HQUIC stream = nullptr;
    {
        std::lock_guard<std::mutex> lk(mtx_);
        auto it = streams_.find(stream_index);
        if (it == streams_.end() || it->second == nullptr) {
            return false;
        }
        stream = it->second;
        closing_streams_.insert(stream);
        streams_.erase(it);
    }

    // Immediate abort
    api_->StreamShutdown(stream, QUIC_STREAM_SHUTDOWN_FLAG_ABORT, 0);
    
    std::cerr << "[msquic] Dynamic stream dropped, index=" << stream_index << "\n";
    return true;
}

bool MsQuicTransport::wait_send_done() {
    std::unique_lock<std::mutex> lk(send_mtx_);
    cv_send_done_.wait_for(lk, std::chrono::seconds(5),
                            [&]{ return send_done_.load(); });
    return send_done_.load();
}


// ------------------- Send -------------------

// send: Sends a NALU fragment through a designated QUIC stream.
// Parameters:
//   stream_index: The index of the target stream (0 for high priority, 1+ for lower priority).
//   hdr: Application-layer packet header (contains nalu_id, fragment_idx, etc.).
//   payload: Pointer to the media payload bytes.
//   is_fin: Whether this packet should set the FIN flag.
bool MsQuicTransport::send(uint32_t stream_index, const moq::PacketHeader& hdr, const uint8_t* payload, bool is_fin) {
    // Allocate and prepare the context OUTSIDE the lock
    moq::PacketHeader hdr_copy = hdr;

    constexpr uint32_t kMaxPayload = 1u << 20; // 1MB restriction for test
    if (hdr.payload_len > kMaxPayload) {
        std::cerr << "[msquic] insane payload_len=" << hdr.payload_len << "\n";
        return false;
    }

    auto* ctx = new SendContext();
    ctx->stream_id = stream_index;
    ctx->nalu_id = hdr.nalu_id;
    ctx->fragment_idx = hdr.fragment_idx;
    ctx->fragment_count = hdr.fragment_count;
    auto now = std::chrono::steady_clock::now().time_since_epoch();
    ctx->enqueue_time_us = std::chrono::duration_cast<std::chrono::microseconds>(now).count();
    ctx->storage.resize(sizeof(hdr) + hdr.payload_len);
    std::memcpy(ctx->storage.data(), &hdr, sizeof(hdr));

    if (hdr.payload_len > 0 && payload) {
        std::memcpy(ctx->storage.data() + sizeof(hdr), payload, hdr.payload_len);
    }

    ctx->qb.Buffer = ctx->storage.data();
    ctx->qb.Length = static_cast<uint32_t>(ctx->storage.size());

    QUIC_SEND_FLAGS flags = QUIC_SEND_FLAG_NONE;
    if (is_fin) {
        flags = QUIC_SEND_FLAG_FIN;
    }

#ifdef ENABLE_SEND_ORDER_LOG
    {
    auto now = std::chrono::steady_clock::now().time_since_epoch();
    auto us = std::chrono::duration_cast<std::chrono::microseconds>(now).count();
    std::cerr << "[SEND_ORDER] t=" << us
              << " stream=" << stream_index
              << " nalu=" << hdr.nalu_id
              << " frag=" << hdr.fragment_idx
              << "/" << hdr.fragment_count
              << " bytes=" << ctx->qb.Length
              << "\n";
}
#endif

#ifdef ENABLE_SEND_LOG
    std::cerr << "[cli] SEND nalu_id=" << hdr.nalu_id
              << " payload_len=" << hdr.payload_len
              << " total_bytes=" << ctx->qb.Length
              << " fin=" << (flags == QUIC_SEND_FLAG_FIN) << "\n";
#endif

    QUIC_STATUS st;
    {
        std::lock_guard<std::mutex> lk(mtx_);
        // Re-check state inside the lock to ensure stream is not freed
        auto it = streams_.find(stream_index);
        if (!connected_.load() || it == streams_.end() || it->second == nullptr) {
            delete ctx;
            return false;
        }
        
        st = api_->StreamSend(
            it->second,
            &ctx->qb,
            1,
            flags,
            ctx);
    }

    // std::cerr << "[msquic] StreamSend st=0x" << std::hex << st << std::dec << "\n";

    if (QUIC_FAILED(st)) {
        std::cerr << "[msquic] StreamSend failed st=0x" << std::hex << st << std::dec << "\n";
        delete ctx;
        return false;
    }

    return true;
}

// ------------------- Run Loop -------------------

void MsQuicTransport::run() {
    // Minimal blocking loop: wait until shutdown.
    running_.store(true);
    std::unique_lock<std::mutex> lk(mtx_);
    cv_.wait(lk, [&] { return !running_.load(); });
}

bool MsQuicTransport::set_stream_priority(uint32_t stream_index, uint16_t priority) {
    std::lock_guard<std::mutex> lk(mtx_);
    auto it = streams_.find(stream_index);
    if (it == streams_.end() || it->second == nullptr) {
        std::cerr << "[msquic] set_stream_priority failed: invalid stream index \n";
        return false;
    }
    QUIC_STATUS st = api_->SetParam(
        it->second,
        QUIC_PARAM_STREAM_PRIORITY,
        sizeof(priority),
        &priority);
    if (QUIC_FAILED(st)) {
        std::cerr << "[msquic] SetParam(PRIORITY) failed st=0x" << std::hex << st << std::dec << "\n";
        return false;
    }
    return true;
}

// ------------------- Callbacks -------------------

QUIC_STATUS QUIC_API
MsQuicTransport::listener_callback(HQUIC /*listener*/, void* context, QUIC_LISTENER_EVENT* event) {
    auto* self = static_cast<MsQuicTransport*>(context);

    switch (event->Type) {
    case QUIC_LISTENER_EVENT_NEW_CONNECTION: {
        HQUIC conn = event->NEW_CONNECTION.Connection;

        // Set connection callback and configuration for accepted connection
        self->api_->SetCallbackHandler(conn, (void*)MsQuicTransport::connection_callback, self);

        QUIC_STATUS st = self->api_->ConnectionSetConfiguration(conn, self->configuration_);
        if (QUIC_FAILED(st)) {
            std::cerr << "[srv] ConnectionSetConfiguration failed st=0x"
                      << std::hex << st << std::dec << "\n";
            return QUIC_STATUS_INTERNAL_ERROR;
        }

        {
            std::lock_guard<std::mutex> lk(self->mtx_);
            self->client_connections_.insert(conn);
        }
        
        std::cerr << "[srv] NEW_CONNECTION accepted conn=" << conn << "\n";
        return QUIC_STATUS_SUCCESS;
    }
    default:
        break;
    }

    return QUIC_STATUS_SUCCESS;
}

QUIC_STATUS QUIC_API
MsQuicTransport::connection_callback(HQUIC connection, void* context, QUIC_CONNECTION_EVENT* event) {
    auto* self = static_cast<MsQuicTransport*>(context);

    // Optional debug
    // std::cerr << "[" << (self->is_client_ ? "cli" : "srv") << "] ConnEvent " << ConnEventName(event->Type) << "\n";

    switch (event->Type) {
    case QUIC_CONNECTION_EVENT_CONNECTED: {
        self->connection_ = connection;
        self->connected_.store(true);

        std::cerr << "[" << (self->is_client_ ? "cli" : "srv")
                  << "] CONNECTED conn=" << connection << "\n";

        // Write TLS key log now that handshake is complete
        if (self->is_client_ && !self->key_log_path_.empty()) {
            self->write_tls_key_log();
        }

        {
            std::lock_guard<std::mutex> lk(self->mtx_);
            self->cv_connected_.notify_all();
        }

        // Client: open streams here
        if (self->is_client_) {
            if (self->streams_.empty() && self->cfg_.num_streams > 0) {
                self->open_and_start_client_streams();
            }
        }
        break;
    }

    case QUIC_CONNECTION_EVENT_PEER_STREAM_STARTED: {
        // Server: peer opened a stream
        HQUIC s = event->PEER_STREAM_STARTED.Stream;
        std::cerr << "[srv] PEER_STREAM_STARTED stream=" << s << "\n";

        self->api_->SetCallbackHandler(s, (void*)MsQuicTransport::stream_callback, self);

        // NOTE: leave default receive mode; do not force StreamReceiveSetEnabled unless necessary.
        self->api_->StreamReceiveSetEnabled(s, TRUE);

        {
            std::lock_guard<std::mutex> lk(self->mtx_);
            uint32_t stream_id = self->next_stream_id_++;
            self->streams_[stream_id] = s;

            std::lock_guard<std::mutex> g(self->stream_states_mtx_);
            auto state = std::make_shared<StreamState>();
            state->stream_id = stream_id;
            self->stream_states_.emplace(s, state);
        }
        break;
    }

    case QUIC_CONNECTION_EVENT_SHUTDOWN_INITIATED_BY_TRANSPORT:
        std::cerr << "[" << (self->is_client_ ? "cli" : "srv")
                  << "] SHUTDOWN_BY_TRANSPORT status="
                  << event->SHUTDOWN_INITIATED_BY_TRANSPORT.Status << "\n";
        break;

    case QUIC_CONNECTION_EVENT_SHUTDOWN_INITIATED_BY_PEER:
        std::cerr << "[" << (self->is_client_ ? "cli" : "srv")
                  << "] SHUTDOWN_BY_PEER error="
                  << event->SHUTDOWN_INITIATED_BY_PEER.ErrorCode << "\n";
        break;

    case QUIC_CONNECTION_EVENT_SHUTDOWN_COMPLETE:
        std::cerr << "[" << (self->is_client_ ? "cli" : "srv") << "] SHUTDOWN_COMPLETE\n";
        if (!self->custom_shutdown_msg_.empty()) {
            std::cout << self->custom_shutdown_msg_;
        }
        self->connected_.store(false);
        {
            std::lock_guard<std::mutex> lk(self->mtx_);
            if (!self->is_client_) {
                self->client_connections_.erase(connection);
            }
            if (self->is_client_) {
                self->running_.store(false);
                self->cv_.notify_all();
            }
            self->cv_connected_.notify_all();
        }
        // NOTE: Do NOT call ConnectionClose here.
        // ConnectionClose is exclusively owned by the destructor.
        // Calling it here creates a data race:
        //   - disconnect(force=True) calls ConnectionShutdown [async]
        //   - This callback fires on a MsQuic thread and would set connection_=nullptr
        //   - Destructor simultaneously reads connection_ on the main thread
        //   - No lock protects connection_, so double-free → SIGABRT
        // The destructor's ConnectionClose already handles everything:
        // if the connection was already shut down, ConnectionClose simply
        // releases the handle without issuing another shutdown.
        break;

    default:
        break;
    }

    return QUIC_STATUS_SUCCESS;
}

QUIC_STATUS QUIC_API
MsQuicTransport::stream_callback(HQUIC stream, void* context, QUIC_STREAM_EVENT* event) {
    auto* self = static_cast<MsQuicTransport*>(context);

#ifdef ENABLE_STREAM_EVENT_LOG
    // Print clean stream event name
    std::cerr << "[" << (self->is_client_ ? "cli" : "srv") << "] StreamEvent " << StreamEventName(event->Type) << " stream=" << stream << "\n";
#endif

    switch (event->Type) {

    case QUIC_STREAM_EVENT_START_COMPLETE: {
        if (self->is_client_) {
            std::cerr << "[cli] STREAM START COMPLETE stream=" << stream
                      << " status=0x" << std::hex << event->START_COMPLETE.Status << std::dec << "\n";
            
            // If StreamStart failed asynchronously (e.g. because ConnectionShutdown was called
            // right after open_stream), MsQuic will NOT deliver SHUTDOWN_COMPLETE.
            // We must close the stream handle here to prevent handle leak & ConnectionClose hang.
            if (QUIC_FAILED(event->START_COMPLETE.Status)) {
                HQUIC strm_to_close = nullptr;
                {
                    std::lock_guard<std::mutex> lk(self->mtx_);
                    for (auto it = self->streams_.begin(); it != self->streams_.end(); ++it) {
                        if (it->second == stream) {
                            strm_to_close = stream;
                            self->streams_.erase(it);
                            break;
                        }
                    }
                }
                if (strm_to_close) {
                    self->api_->StreamClose(strm_to_close);
                }
            }

            self->stream_ready_.store(true);
            {
                std::lock_guard<std::mutex> lk(self->mtx_stream_ready_);
                self->cv_stream_ready_.notify_all();
            }
        }
        break;
    }

    case QUIC_STREAM_EVENT_RECEIVE: {

        auto tid = (uint64_t)pthread_self();

        std::cerr << "\n[srv] RECEIVE BEGIN"
                  << " tid=" << tid
                  << " stream=" << stream
                  << " event=" << event
                  << " total=" << event->RECEIVE.TotalBufferLength
                  << " bufcnt=" << event->RECEIVE.BufferCount
                  << "\n";

        uint64_t consumed = 0;

        // old code
        // for (uint32_t i = 0; i < event->RECEIVE.BufferCount; ++i) {

        //     const uint8_t* buf =
        //         event->RECEIVE.Buffers[i].Buffer;

        //     uint32_t len =
        //         event->RECEIVE.Buffers[i].Length;

        //     handle_stream_receive(stream, buf, len);
        // }
        
        self->handle_stream_receive(
             stream,
             event->RECEIVE.Buffers,
             event->RECEIVE.BufferCount);

        std::cerr << "[srv] about to complete consumed=" << consumed
                  << " total=" << event->RECEIVE.TotalBufferLength
                  << "\n";

        // self->api_->StreamReceiveComplete(
        //     stream,
        //     event->RECEIVE.TotalBufferLength
        // );

        std::cerr << "[srv] COMPLETE CALLED\n";

        break;
    }

    case QUIC_STREAM_EVENT_SEND_COMPLETE: {
        auto* ctx = static_cast<SendContext*>(event->SEND_COMPLETE.ClientContext);

        if (self->is_client_) {
            // Always log canceled sends — this is the key observable signal that
            // StreamShutdown(ABORT) / drop_stream discarded buffered data.
            if (event->SEND_COMPLETE.Canceled) {
                std::cerr << "[cli] SEND_COMPLETE CANCELED stream=" << stream
                          << " nalu=" << ctx->nalu_id
                          << " frag=" << ctx->fragment_idx
                          << "/" << ctx->fragment_count << "\n";
            }
#ifdef ENABLE_ACK_ORDER_LOG
            {
                auto now = std::chrono::steady_clock::now().time_since_epoch();
                auto us = std::chrono::duration_cast<std::chrono::microseconds>(now).count();
                std::cerr << "[ACK_ORDER] t=" << us
                          << " nalu=" << ctx->nalu_id
                          << " frag=" << ctx->fragment_idx
                          << "/" << ctx->fragment_count
                          << " canceled=" << (event->SEND_COMPLETE.Canceled ? 1 : 0) << "\n";
            }
#endif

            {
                std::lock_guard<std::mutex> lk(self->mtx_send_);
                self->send_complete_count_++;
            }
            self->cv_send_done_.notify_all();
        }

        if (self->ack_cb_) {
            moq::transport::AckEvent ack;
            ack.stream_id = ctx->stream_id;
            ack.frame_id = ctx->nalu_id;
            ack.fragment_idx = ctx->fragment_idx;
            ack.canceled = event->SEND_COMPLETE.Canceled;
            
            auto now = std::chrono::steady_clock::now().time_since_epoch();
            auto now_us = std::chrono::duration_cast<std::chrono::microseconds>(now).count();
            if (now_us > ctx->enqueue_time_us) {
                ack.delay_us = now_us - ctx->enqueue_time_us;
            } else {
                ack.delay_us = 0;
            }
            
            self->ack_cb_(ack);
        }

        delete ctx;
        break;
    }

    case QUIC_STREAM_EVENT_PEER_SEND_SHUTDOWN: {
        std::cerr << "[srv] PEER_SEND_SHUTDOWN stream=" << stream << "\n";
        break;
    }

    case QUIC_STREAM_EVENT_PEER_SEND_ABORTED: {
        std::cerr << "[srv] PEER_SEND_ABORTED error="
                  << event->PEER_SEND_ABORTED.ErrorCode
                  << " stream=" << stream << "\n";
        break;
    }

    case QUIC_STREAM_EVENT_SHUTDOWN_COMPLETE: {
        std::cerr << "[" << (self->is_client_ ? "cli" : "srv")
                  << "] STREAM SHUTDOWN_COMPLETE stream=" << stream << "\n";

        // AppCloseInProgress=true means StreamClose was already called by the App
        // (destructor or drop_stream path). Do NOT call StreamClose again.
        if (!event->SHUTDOWN_COMPLETE.AppCloseInProgress) {
            HQUIC strm_to_close = nullptr;
            {
                std::lock_guard<std::mutex> lk(self->mtx_);
                // Search active streams (pre-shutdown send path)
                for (auto it = self->streams_.begin(); it != self->streams_.end(); ++it) {
                    if (it->second == stream) {
                        strm_to_close = stream;
                        self->streams_.erase(it);
                        break;
                    }
                }
                // Also search gracefully-closing streams (close_stream path)
                if (!strm_to_close) {
                    auto it = self->closing_streams_.find(stream);
                    if (it != self->closing_streams_.end()) {
                        strm_to_close = stream;
                        self->closing_streams_.erase(it);
                    }
                }
            }
            if (strm_to_close) {
                self->api_->StreamClose(strm_to_close);
            }
        }

        if (self->is_client_) {
            std::lock_guard<std::mutex> lk(self->mtx_);
            if (self->streams_.empty() && self->closing_streams_.empty()) {
                self->cv_stream_done_.notify_all();
            }
        }
        break;
    }

    default:
        break;
    }

    return QUIC_STATUS_SUCCESS;
}

// ------------------- Framing (header -> payload) -------------------

void MsQuicTransport::handle_stream_receive(HQUIC stream, const QUIC_BUFFER* bufs, uint32_t buf_count) {
    for (uint32_t i = 0; i < buf_count; ++i) {
        const uint8_t* data = bufs[i].Buffer;
        size_t len = bufs[i].Length;
        if (data && len) handle_stream_bytes(stream, data, len);
    }
}

// handle_stream_bytes: Processes a continuous raw byte stream received from a QUIC stream.
// Since QUIC is stream-based, we must frame/reassemble the incoming data into PacketHeader + Payload packets.
// 1. Append the newly received bytes to the stream-specific accumulator buffer.
// 2. Check if the buffer has enough bytes to read a complete 16-byte PacketHeader.
// 3. If so, parse the header to get `payload_len`, and wait until the buffer contains the full payload.
// 4. Once a complete packet is received, invoke `recv_cb_` to deliver it to the upper layer (Reassembler).
void MsQuicTransport::handle_stream_bytes(HQUIC stream, const uint8_t* data, size_t len) {
    std::shared_ptr<StreamState> state_ptr;

    {
        std::lock_guard<std::mutex> g(stream_states_mtx_);
        auto it = stream_states_.find(stream);
        if (it == stream_states_.end()) {
            it = stream_states_.emplace(stream, std::make_shared<StreamState>()).first;
        }
        state_ptr = it->second;
    }

    auto& state = *state_ptr;

    // Append bytes
    state.buffer.insert(state.buffer.end(), data, data + len);

    // Robust parse loop
    while (true) {
        if (state.reading_header) {
            if (state.buffer.size() - state.read_offset < sizeof(moq::PacketHeader)) return;

            std::memcpy(&state.current_header, state.buffer.data() + state.read_offset, sizeof(moq::PacketHeader));
            state.read_offset += sizeof(moq::PacketHeader);
            
            state.reading_header = false;
            state.bytes_needed = state.current_header.payload_len;
        }

        // Safety guard against absurd lengths
        if (!state.reading_header && state.bytes_needed > (1u << 26)) {
            std::cerr << "[msquic] Invalid payload_len=" << state.bytes_needed
                      << ", aborting stream\n";
            api_->StreamShutdown(stream, QUIC_STREAM_SHUTDOWN_FLAG_ABORT, 0);
            return;
        }

        if (!state.reading_header) {
            if (state.buffer.size() - state.read_offset < state.bytes_needed) return;

            std::vector<uint8_t> payload(
                state.buffer.begin() + state.read_offset,
                state.buffer.begin() + state.read_offset + state.bytes_needed);

            if (recv_cb_) {
#ifdef ENABLE_RECV_ORDER_LOG
                {
                    auto now = std::chrono::steady_clock::now().time_since_epoch();
                    auto us = std::chrono::duration_cast<std::chrono::microseconds>(now).count();
                    std::cerr << "[RECV_ORDER] t=" << us
                              << " nalu=" << state.current_header.nalu_id
                              << " frag=" << state.current_header.fragment_idx
                              << "/" << state.current_header.fragment_count
                              << " bytes=" << state.current_header.payload_len << "\n";
                }
#endif
                recv_cb_(state.stream_id, state.current_header, payload.data(), payload.size());
            }

            state.read_offset += state.bytes_needed;

            // Memory compaction: erase if offset is large (e.g. > 1MB) to prevent O(n) on every frame
            if (state.read_offset > 1024 * 1024) {
                state.buffer.erase(state.buffer.begin(), state.buffer.begin() + state.read_offset);
                state.read_offset = 0;
            }

            // Next header
            state.reading_header = true;
            state.bytes_needed = sizeof(moq::PacketHeader);
        }
    }
}

// ------------------- Client send wait helpers -------------------

void MsQuicTransport::set_send_expected(uint32_t n) {
    send_complete_count_ = 0;
    send_expected_ = n;
}

bool MsQuicTransport::wait_all_sends_done(uint32_t timeout_ms) {
    std::unique_lock<std::mutex> lk(mtx_send_);
    return cv_send_done_.wait_for(
        lk,
        std::chrono::milliseconds(timeout_ms),
        [&] { return send_complete_count_.load() >= send_expected_; });
}



void MsQuicTransport::disconnect(bool force) {
    if (!is_client_) return;
    
    if (force) {
        if (connection_) {
            api_->ConnectionShutdown(connection_, QUIC_CONNECTION_SHUTDOWN_FLAG_SILENT, 0);
        }
    } else {
        // Graceful
        std::lock_guard<std::mutex> lk(mtx_);
        for (auto& [id, s] : streams_) {
            if (s) {
                api_->StreamShutdown(s, QUIC_STREAM_SHUTDOWN_FLAG_GRACEFUL, 0);
            }
        }
        wait_stream_shutdown(5000);
    }
}

void MsQuicTransport::stop_server(bool force) {
    if (is_client_) return;

    if (listener_) {
        api_->ListenerStop(listener_);
    }

    std::lock_guard<std::mutex> lk(mtx_);
    for (HQUIC conn : client_connections_) {
        QUIC_CONNECTION_SHUTDOWN_FLAGS flags = force ? QUIC_CONNECTION_SHUTDOWN_FLAG_SILENT : QUIC_CONNECTION_SHUTDOWN_FLAG_NONE;
        api_->ConnectionShutdown(conn, flags, 0);
    }
    running_.store(false);
    cv_.notify_all();
}

bool MsQuicTransport::wait_stream_shutdown(uint32_t timeout_ms) {
    std::unique_lock<std::mutex> lk(mtx_);
    return cv_stream_done_.wait_for(
        lk,
        std::chrono::milliseconds(timeout_ms),
        [&] { return streams_.empty() && closing_streams_.empty(); });
}

} // namespace moq::transport

// ------------------- TLS Key Log -------------------

namespace moq::transport {

// Helper: convert byte array to hex string
static std::string bytes_to_hex(const uint8_t* buf, size_t len) {
    static const char hex_chars[] = "0123456789abcdef";
    std::string out;
    out.reserve(len * 2);
    for (size_t i = 0; i < len; ++i) {
        out += hex_chars[(buf[i] >> 4) & 0xF];
        out += hex_chars[buf[i] & 0xF];
    }
    return out;
}

void MsQuicTransport::write_tls_key_log() {
    // MsQuic writes directly into tls_secrets_ during handshake (via the SetParam pointer).
    // QUIC_PARAM_CONN_TLS_SECRETS is Set-only; no GetParam needed.
    std::ofstream f(key_log_path_, std::ios::app);
    if (!f.is_open()) {
        std::cerr << "[msquic] Cannot open key log file: " << key_log_path_ << "\n";
        return;
    }

    // NSS Key Log format: <LABEL> <ClientRandom> <Secret>
    // Wireshark understands these labels for QUIC/TLS 1.3:
    //   CLIENT_HANDSHAKE_TRAFFIC_SECRET
    //   SERVER_HANDSHAKE_TRAFFIC_SECRET
    //   CLIENT_TRAFFIC_SECRET_0
    //   SERVER_TRAFFIC_SECRET_0
    //   CLIENT_EARLY_TRAFFIC_SECRET  (0-RTT, may be empty)

    const std::string rand_hex = bytes_to_hex(tls_secrets_.ClientRandom, 32);
    const size_t slen = tls_secrets_.SecretLength;

    auto write_line = [&](const char* label, const uint8_t* secret) {
        f << label << " " << rand_hex << " "
          << bytes_to_hex(secret, slen) << "\n";
    };

    if (tls_secrets_.IsSet.ClientEarlyTrafficSecret)
        write_line("CLIENT_EARLY_TRAFFIC_SECRET",
                   tls_secrets_.ClientEarlyTrafficSecret);
    if (tls_secrets_.IsSet.ClientHandshakeTrafficSecret)
        write_line("CLIENT_HANDSHAKE_TRAFFIC_SECRET",
                   tls_secrets_.ClientHandshakeTrafficSecret);
    if (tls_secrets_.IsSet.ServerHandshakeTrafficSecret)
        write_line("SERVER_HANDSHAKE_TRAFFIC_SECRET",
                   tls_secrets_.ServerHandshakeTrafficSecret);
    if (tls_secrets_.IsSet.ClientTrafficSecret0)
        write_line("CLIENT_TRAFFIC_SECRET_0",
                   tls_secrets_.ClientTrafficSecret0);
    if (tls_secrets_.IsSet.ServerTrafficSecret0)
        write_line("SERVER_TRAFFIC_SECRET_0",
                   tls_secrets_.ServerTrafficSecret0);

    f.flush();
    std::cerr << "[msquic] TLS keys written to " << key_log_path_ << "\n";
}
void MsQuicTransport::set_custom_shutdown_message(const std::string& msg) {
    custom_shutdown_msg_ = msg;
}

void MsQuicTransport::print_connection_statistics() {
    if (!connection_) return;

    QUIC_STATISTICS stats{};
    uint32_t stats_len = sizeof(stats);
    QUIC_STATUS st = api_->GetParam(
        connection_,
        QUIC_PARAM_CONN_STATISTICS,
        &stats_len,
        &stats);

    if (st == QUIC_STATUS_SUCCESS) {
        std::cout << "\n--- MsQuic Connection Statistics ---\n";
        std::cout << "Send Total Packets:          " << stats.Send.TotalPackets << "\n";
        std::cout << "Send Suspected Lost Packets: " << stats.Send.SuspectedLostPackets << "\n";
        std::cout << "Send Spurious Lost Packets:  " << stats.Send.SpuriousLostPackets << "\n";
        std::cout << "Recv Total Packets:          " << stats.Recv.TotalPackets << "\n";
        std::cout << "Recv Dropped Packets:        " << stats.Recv.DroppedPackets << "\n";
        std::cout << "------------------------------------\n" << std::flush;
    } else {
        std::cerr << "Failed to get connection statistics, status: 0x" << std::hex << st << std::dec << "\n" << std::flush;
    }
}

NetworkStats MsQuicTransport::get_network_stats() const {
    NetworkStats stats;
    if (!connection_ || !api_) {
        return stats; // empty
    }

    QUIC_STATISTICS_V2 quic_stats{};
    uint32_t stats_len = sizeof(quic_stats);
    QUIC_STATUS st = api_->GetParam(
        connection_,
        QUIC_PARAM_CONN_STATISTICS_V2,
        &stats_len,
        &quic_stats
    );

    if (QUIC_SUCCEEDED(st)) {
        stats.rtt_us = quic_stats.Rtt;
        stats.min_rtt_us = quic_stats.MinRtt;
        stats.max_rtt_us = quic_stats.MaxRtt;
        stats.rtt_variance_us = quic_stats.RttVariance;
        stats.send_path_mtu = quic_stats.SendPathMtu;
        stats.cwnd_bytes = quic_stats.SendCongestionWindow;
        stats.bytes_sent = quic_stats.SendTotalBytes;
        stats.bytes_received = quic_stats.RecvTotalBytes;
        stats.send_total_stream_bytes = quic_stats.SendTotalStreamBytes;
        stats.recv_total_stream_bytes = quic_stats.RecvTotalStreamBytes;
        stats.packets_sent = quic_stats.SendTotalPackets;
        stats.packets_lost = quic_stats.SendSuspectedLostPackets;
        stats.send_spurious_lost_packets = quic_stats.SendSpuriousLostPackets;
        stats.send_congestion_count = quic_stats.SendCongestionCount;
    }

    QUIC_NETWORK_STATISTICS net_stats{};
    uint32_t net_stats_len = sizeof(net_stats);
    QUIC_STATUS st2 = api_->GetParam(
        connection_,
        QUIC_PARAM_CONN_NETWORK_STATISTICS,
        &net_stats_len,
        &net_stats
    );

    if (QUIC_SUCCEEDED(st2)) {
        stats.estimated_bandwidth_bps = net_stats.Bandwidth;
        stats.bytes_in_flight = net_stats.BytesInFlight;
        stats.posted_bytes = net_stats.PostedBytes;
        stats.ideal_bytes = net_stats.IdealBytes;
    } else {
        std::cerr << "GetParam(QUIC_PARAM_CONN_NETWORK_STATISTICS) failed with status: " << st2 << std::endl;
    }

    return stats;
}

} // namespace moq::transport
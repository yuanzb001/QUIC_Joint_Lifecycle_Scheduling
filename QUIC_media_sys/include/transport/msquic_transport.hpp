// include/moq/transport/msquic_transport.hpp
#pragma once
#include "transport/quic_transport.hpp"
#include <msquic.h>

#include <atomic>
#include <condition_variable>
#include <fstream>
#include <mutex>
#include <string>
#include <unordered_map>
#include <unordered_set>
#include <vector>

namespace moq::transport {

/**
 * @brief Concrete implementation of IQuicTransport using Microsoft's MsQuic library.
 * 
 * Provides features such as connection lifecycle management, stream prioritization,
 * TLS key capture/export, framing stream bytes, and thread-safe signaling synchronization.
 */
class MsQuicTransport final : public IQuicTransport {
public:
    /**
     * @brief Constructor. Opens the MsQuic API table and registers the application configuration registration.
     */
    MsQuicTransport();

    /**
     * @brief Destructor. Closes all open streams, connection configurations, listeners, and releases API handles.
     */
    ~MsQuicTransport() override;

    /**
     * @brief Registers the callback for incoming packet events.
     * @param cb Callback function matching PacketRecvCb.
     */
    void set_recv_callback(PacketRecvCb cb) override;

    /**
     * @brief Registers the callback for ACK events.
     * @param cb Callback function matching AckCb.
     */
    void set_ack_callback(AckCb cb) override;
    
    NetworkStats get_network_stats() const override;

    /**
     * @brief Blocks current thread execution until the connection is successfully established.
     * @return true if connected, false if initialization was aborted or failed.
     */
    bool wait_connected();

    /**
     * @brief Starts the server in listening mode on the specified port.
     * @param cfg Configuration structure details.
     * @return true if listener started, false otherwise.
     */
    bool start_server(const QuicConfig& cfg) override;

    /**
     * @brief Starts the client connection and begins the TLS handshake to host.
     * @param cfg Configuration structure details.
     * @return true if connection initialization succeeded, false otherwise.
     */
    bool start_client(const QuicConfig& cfg) override;

    /**
     * @brief Send application-layer packet through designated stream.
     * @param stream_index Index of the target stream (0 to num_streams - 1).
     * @param hdr Header metadata of the packet being sent.
     * @param payload Pointer to the payload data segment.
     * @param is_fin Boolean flag indicating if this is the final send on the stream.
     * @return true if stream send event was scheduled successfully, false otherwise.
     */
    bool send(uint32_t stream_index, const moq::PacketHeader& hdr, const uint8_t* payload, bool is_fin = false) override;

    /**
     * @brief Configures priority settings of QUIC stream to direct bandwidth allocation.
     * @param stream_index Index of the target stream (0 to num_streams - 1).
     * @param priority 16-bit priority value (e.g. 0xFFFF for critical, 0x0001 for low priority).
     * @return true on success, false on failure.
     */
    bool set_stream_priority(uint32_t stream_index, uint16_t priority);

    /**
     * @brief Enters block wait loop to hold the execution thread alive during communication.
     */
    void run() override;

    /**
     * @brief Helper block to wait until next single send completes.
     * @return true if completed, false if timeout expires.
     */
    bool wait_send_done();

    /**
     * @brief Dynamically opens and starts a new QUIC stream.
     * @param priority Initial priority of the stream.
     * @return The stream index (ID) on success, or -1 on failure.
     */
    int32_t open_stream(uint16_t priority = 0);

    /**
     * @brief Dynamically closes an existing QUIC stream.
     * @param stream_index The index of the stream to close.
     * @return true on success.
     */
    bool close_stream(uint32_t stream_index);

    /**
     * @brief Sets the total count of packets expected to be sent.
     * @param n Expected packet count.
     */
    void set_send_expected(uint32_t n);

    /**
     * @brief Waits for all expected packet sending operations to receive a SEND_COMPLETE ACK.
     * @param timeout_ms Timeout threshold in milliseconds.
     * @return true if all packets were acknowledged, false if timeout was reached.
     */
    bool wait_all_sends_done(uint32_t timeout_ms = 5000);

    /**
     * @brief Instructs transport to attach a FIN bit flag to the next stream send call.
     * @param fin Boolean flag toggle.
     */
    void set_next_send_fin(bool fin);

    /**
     * @brief Wait for the client streams to be initialized and ready.
     */
    bool wait_stream_ready();

    /**
     * @brief Aborts a QUIC stream and discards all buffered data.
     * @param stream_index Index of the stream to drop.
     * @return true if stream index is valid.
     */
    bool drop_stream(uint32_t stream_index);

    /**
     * @brief Initiates graceful stream shutdown on all active streams (Client only).
     * @param force If true, forcefully aborts streams; otherwise, waits for graceful shutdown.
     */
    void disconnect(bool force = false);

    /**
     * @brief Stops the server listener and cleanly shuts down all active client connections.
     * @param force If true, forcefully aborts connections; otherwise, shuts down gracefully.
     */
    void stop_server(bool force = false);

    /**
     * @brief Blocks execution until all streams have completely shut down.
     * @param timeout_ms Timeout threshold in milliseconds.
     * @return true if streams shut down cleanly, false on timeout.
     */
    bool wait_stream_shutdown(uint32_t timeout_ms);

    /**
     * @brief Prints connection statistics (dropped packets, RTT, etc).
     */
    void print_connection_statistics();

    /**
     * @brief Sets a custom message to be printed during connection shutdown, right before statistics.
     */
    void set_custom_shutdown_message(const std::string& msg);

private:
    std::string custom_shutdown_msg_;                 ///< Custom message to print on shutdown
    
    // ===== send completion tracking =====
    std::mutex mtx_send_;                             ///< Mutex for send synchronizations.
    std::condition_variable cv_send_done_;            ///< CV signaling all sends completed.
    std::atomic<uint32_t> send_expected_{0};          ///< Expected total number of sends.
    std::atomic<uint32_t> send_complete_count_{0};    ///< Actual completed sends count.
    
    std::mutex send_mtx_;                             ///< Mutex for single-send signaling synchronization.
    std::atomic<bool> send_done_{false};               ///< Single-send completion state.
    bool is_client_ = false;                          ///< True if this transport represents a client connection.
    QUIC_CERTIFICATE_FILE cert_file_;                 ///< Holds references to the server TLS certificate paths.
    
    // ===== msquic handles =====
    const QUIC_API_TABLE* api_{nullptr};              ///< Table containing MsQuic API function pointers.
    HQUIC registration_{nullptr};                     ///< Registration context handle.
    HQUIC configuration_{nullptr};                    ///< Configuration context handle (holds ALPN, settings, creds).
    HQUIC listener_{nullptr};                         ///< Server listener context handle.
    HQUIC connection_{nullptr};                       ///< Client connection context handle.
    std::unordered_set<HQUIC> client_connections_;    ///< Active client connections (Server only).

    std::unordered_map<uint32_t, HQUIC> streams_;     ///< Client-side sending streams mapped by application-level unique stream ID.
    uint32_t next_stream_id_{1};                      ///< Monotonically increasing unique ID for new streams.
    std::unordered_set<HQUIC> closing_streams_;       ///< Streams undergoing graceful shutdown (StreamShutdown called, awaiting SHUTDOWN_COMPLETE before StreamClose).
    PacketRecvCb recv_cb_;                             ///< Registered packet receive callback.
    AckCb ack_cb_;                                    ///< Registered ACK callback.

    QuicConfig cfg_{};                                ///< Active connection configuration settings.

    // ===== synchronization =====
    std::atomic<bool> running_{false};                ///< State tracker for the block wait loop.
    std::atomic<bool> connected_{false};              ///< State tracker representing active connection status.
    std::condition_variable cv_;                      ///< CV to keep the main runner thread blocked.

    std::mutex mtx_;                                  ///< Mutex for connection state synchronization.
    std::condition_variable cv_connected_;            ///< CV for connection state synchronization.

    std::atomic<bool> stream_ready_{false};           ///< State tracker representing streams setup ready status.
    std::condition_variable cv_stream_ready_;         ///< CV for stream readiness signaling.
    std::mutex mtx_stream_ready_;                     ///< Mutex for stream readiness signaling.
    std::mutex stream_states_mtx_;                    ///< Mutex protecting stream states map access.

    // class members
    std::condition_variable cv_stream_done_;          ///< CV for stream shutdown synchronization.

    std::atomic<uint64_t> total_recv_bytes_{0};       ///< Count of total received bytes from stream callbacks.

    // ===== TLS key log (for Wireshark QUIC decryption) =====
    QUIC_TLS_SECRETS tls_secrets_{};                  ///< Captures TLS secrets during the handshake.
    std::string key_log_path_;                        ///< Target path where the SSLKEYLOGFILE format is exported.
    void write_tls_key_log();                         ///< Writes captured TLS secrets to disk in NSS key log format.

    // ===== per-stream receive state =====
    struct StreamState {
        uint32_t stream_id = 0;                      ///< Application-level stream ID.
        std::vector<uint8_t> buffer;                 ///< Accumulates incoming byte fragments.
        size_t read_offset = 0;                      ///< Current read position in buffer to avoid O(n) erase.
        bool reading_header = true;                  ///< State flag indicating if we are parsing the 16-byte header.
        moq::PacketHeader current_header{};          ///< Temporarily stores the current parsed header.
        size_t bytes_needed = sizeof(moq::PacketHeader); ///< Count of bytes needed to proceed to next parsing step.
    };
    std::unordered_map<HQUIC, std::shared_ptr<StreamState>> stream_states_; ///< Map correlating stream handles to parser buffers.

    // ===== send context (keep buffer alive until SEND_COMPLETE) =====
    struct SendContext {
        QUIC_BUFFER qb{};                            ///< MsQuic buffer structure pointing to the memory space.
        std::vector<uint8_t> storage;                ///< Backing memory buffer keeping raw header+payload alive.
        uint32_t stream_id{0};                       ///< Application-level stream ID.
        uint32_t nalu_id{0};                         ///< Parent NALU ID (for logging/ACK tracking).
        uint16_t fragment_idx{0};                    ///< Current fragment index (for logging/ACK tracking).
        uint16_t fragment_count{0};                  ///< Total fragment count (for logging/ACK tracking).
        uint64_t enqueue_time_us{0};                 ///< Timestamp when the packet was enqueued.
    };

    // ===== helpers =====
    bool init_configuration_client();                 ///< Internal setup for client configuration credentials.
    bool init_configuration_server();                 ///< Internal setup for server configurations (credentials and flow limits).
    bool open_and_start_client_streams();             ///< Opens pre-defined client streams after connection completes.

    /**
     * @brief Iterates incoming buffers and passes bytes to handle_stream_bytes.
     */
    void handle_stream_receive(HQUIC stream, const QUIC_BUFFER* bufs, uint32_t buf_count);
    
    /**
     * @brief Parses the raw stream byte stream into structured packet headers and payloads.
     */
    void handle_stream_bytes(HQUIC stream, const uint8_t* data, size_t len);
    

    // ===== callbacks =====
    static QUIC_STATUS QUIC_API listener_callback(HQUIC listener, void* context, QUIC_LISTENER_EVENT* event);
    static QUIC_STATUS QUIC_API connection_callback(HQUIC connection, void* context, QUIC_CONNECTION_EVENT* event);
    static QUIC_STATUS QUIC_API stream_callback(HQUIC stream, void* context, QUIC_STREAM_EVENT* event);
};

} // namespace moq::transport
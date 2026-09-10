#include <iostream>
#include <string>
#include <thread>
#include <chrono>

#include "app/reassembler.hpp"
#include "app/sinks.hpp"
#include "transport/msquic_transport.hpp"

#include <atomic>

// Global stats
std::atomic<uint64_t> high_bytes_sec{0};
std::atomic<uint64_t> low_bytes_sec{0};
std::atomic<uint64_t> last_high_time{0};
std::atomic<uint64_t> last_low_time{0};
std::atomic<uint64_t> high_max_jitter{0};
std::atomic<uint64_t> low_max_jitter{0};

// Main program entry point (moq_server)
// Responsibilities:
// 1. Initialize FileSink (to output reassembled video stream data to a file).
// 2. Initialize Reassembler (to buffer, reorder, and reassemble out-of-order packet fragments by nalu_id and fragment_idx).
// 3. Initialize MsQuicTransport and register the receive callback handler.
// 4. Feed incoming packets received by the transport layer into the Reassembler.
// 5. Start the QUIC Server listener and run the processing loop.
int main(int argc, char** argv) {
    if (argc < 6) {
        std::cerr << "Usage: moq_server <host> <port> <output.bin> <cert> <key> [--min_mtu=N] [--max_mtu=N]\n";
        return 1;
    }

    uint16_t min_mtu = 0;
    uint16_t max_mtu = 0;

    for (int i = 6; i < argc; ++i) {
        std::string arg = argv[i];
        if (arg.find("--min_mtu=") == 0) {
            min_mtu = static_cast<uint16_t>(std::stoul(arg.substr(10)));
        } else if (arg.find("--max_mtu=") == 0) {
            max_mtu = static_cast<uint16_t>(std::stoul(arg.substr(10)));
        } else {
            std::cerr << "Unknown parameter: " << arg << "\n";
            std::cerr << "Usage: moq_server <host> <port> <output.bin> <cert> <key> [--min_mtu=N] [--max_mtu=N]\n";
            return 1;
        }
    }

    moq::transport::QuicConfig cfg;
    cfg.host = argv[1];
    cfg.port = static_cast<uint16_t>(std::stoi(argv[2]));
    std::string out_path = argv[3];

    cfg.cert_file = argv[4];
    cfg.key_file  = argv[5];
    
    cfg.min_mtu = min_mtu;
    cfg.max_mtu = max_mtu;


    moq::app::FileSink sink(out_path);
    moq::app::Reassembler reasm([&](const uint8_t* data, size_t len) {
        sink.write(data, len);
    });

    moq::transport::MsQuicTransport quic;
    std::atomic<uint32_t> packets_received{0};
    quic.set_recv_callback(
        [&](const moq::PacketHeader& hdr,
            const uint8_t* payload,
            size_t len)
        {
            uint32_t current = ++packets_received;
            
            // Calc stats
            auto now_ms = std::chrono::duration_cast<std::chrono::milliseconds>(
                std::chrono::system_clock::now().time_since_epoch()).count();
            
            if (len > 4) {
                std::string prefix((const char*)payload, 4);
                if (prefix == "HIGH") {
                    high_bytes_sec += len;
                    uint64_t last = last_high_time.exchange(now_ms);
                    if (last > 0) {
                        uint64_t jitter = now_ms - last;
                        uint64_t current_max = high_max_jitter.load();
                        while (jitter > current_max && !high_max_jitter.compare_exchange_weak(current_max, jitter));
                    }
                } else if (prefix == "LOW_") {
                    low_bytes_sec += len;
                    uint64_t last = last_low_time.exchange(now_ms);
                    if (last > 0) {
                        uint64_t jitter = now_ms - last;
                        uint64_t current_max = low_max_jitter.load();
                        while (jitter > current_max && !low_max_jitter.compare_exchange_weak(current_max, jitter));
                    }
                }
            }

            if (current % 1000 == 0) {
                // std::cout << "[moq_server] Received " << current << " packets so far...\n";
            }
            reasm.on_packet(hdr, payload, len);
        });

    if (!quic.start_server(cfg)) {
        std::cerr << "QUIC server start failed.\n";
        return 2;
    }

    std::thread timer_thread([&]() {
        while (true) {
            std::this_thread::sleep_for(std::chrono::seconds(1));
            uint64_t h_bytes = high_bytes_sec.exchange(0);
            uint64_t l_bytes = low_bytes_sec.exchange(0);
            uint64_t h_jitter = high_max_jitter.exchange(0);
            uint64_t l_jitter = low_max_jitter.exchange(0);
            
            if (h_bytes > 0 || l_bytes > 0) {
                std::cout << "\n=========================================\n"
                          << "[Recv] Throughput - HIGH: " << (h_bytes / 1024) << " KB/s | LOW: " << (l_bytes / 1024) << " KB/s\n"
                          << "[Recv] Max Jitter - HIGH: " << h_jitter << " ms | LOW: " << l_jitter << " ms\n"
                          << "=========================================\n";
            }
        }
    });
    timer_thread.detach();

    quic.run();
    std::cout << "\n>>> [moq_server] " << packets_received.load() << " Application-Layer packets received.\n\n";
    sink.flush();
    return 0;
}
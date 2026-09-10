#include <cstdio>
#include <cstring>
#include <fstream>
#include <iostream>
#include <vector>
#include <thread>
#include <chrono>
#include <cmath>

#include "app/packetizer.hpp"
#include "app/scheduler.hpp"
#include "transport/msquic_transport.hpp"
#include "app/h265_parser.hpp"  // H.265
#include "app/av1_parser.hpp"   // AV1 / IVF
#include <memory>

/* old code
static size_t file_reader(std::ifstream& ifs, uint8_t* dst, size_t max_len) {
    if (!ifs) return 0;
    ifs.read(reinterpret_cast<char*>(dst), static_cast<std::streamsize>(max_len));
    return static_cast<size_t>(ifs.gcount());
}
 */

// Main program entry point (moq_client)
// Responsibilities:
// 1. Read and parse the raw H.265 annex-B video stream file.
// 2. Configure QUIC connection parameters (including MTU constraints and stream allocations).
// 3. Establish and start the MsQuic connection, waiting for client streams to become ready.
// 4. Configure Stream Priorities (assigning Stream 0 as highest priority, others as low priority).
// 5. Use the Packetizer to split large NALUs into MTU-compliant application-layer fragments.
// 6. Route fragments to appropriate streams based on NALU classification and priority rules.
// 7. Block wait until all packets receive send acknowledgments, then execute a clean shutdown.
int main(int argc, char** argv) {
    if (argc < 4) {
        std::cerr << "Usage: moq_client <host> <port> <input.h265/ivf> [--streams=N] [--chunksize=N] [--min_mtu=N] [--max_mtu=N]\n";
        return 1;
    }

    const std::string host = argv[1];
    const uint16_t port = static_cast<uint16_t>(std::stoi(argv[2]));
    const std::string input_path = argv[3];

    // Default values
    uint32_t num_streams = 2;
    size_t max_payload_size = 1200;
    uint16_t min_mtu = 0;
    uint16_t max_mtu = 0;
    uint32_t pacing_delay_ms = 0;

    // Parse optional named parameters
    for (int i = 4; i < argc; ++i) {
        std::string arg = argv[i];
        if (arg.find("--streams=") == 0) {
            num_streams = static_cast<uint32_t>(std::stoul(arg.substr(10)));
        } else if (arg.find("--chunksize=") == 0) {
            max_payload_size = static_cast<size_t>(std::stoul(arg.substr(12)));
        } else if (arg.find("--min_mtu=") == 0) {
            min_mtu = static_cast<uint16_t>(std::stoul(arg.substr(10)));
        } else if (arg.find("--max_mtu=") == 0) {
            max_mtu = static_cast<uint16_t>(std::stoul(arg.substr(10)));
        } else if (arg.find("--delay=") == 0) {
            pacing_delay_ms = static_cast<uint32_t>(std::stoul(arg.substr(8)));
        } else {
            std::cerr << "Unknown parameter: " << arg << "\n";
            std::cerr << "Usage: moq_client <host> <port> <input> [--streams=N] [--chunksize=N] [--min_mtu=N] [--max_mtu=N] [--delay=N]\n";
            return 1;
        }
    }
    
    if (num_streams > 64) {
        std::cerr << "Error: num_streams (" << num_streams << ") exceeds reasonable limit (64).\n";
        return 1;
    }

    /* old code
    const size_t chunk_size = (argc >= 6) ? static_cast<size_t>(std::stoul(argv[5])) : 1200;

    std::ifstream ifs(input_path, std::ios::binary);
    if (!ifs) {
        std::cerr << "Failed to open input: " << input_path << "\n";
        return 1;
    }
    */

    // Determine file type from extension
    bool is_ivf = (input_path.length() >= 4 && input_path.substr(input_path.length() - 4) == ".ivf");

    std::vector<moq::app::Nalu> h265_nalus;
    std::vector<moq::app::Av1Frame> av1_frames;

    // Initialize parser based on file extension
    // parse: Extract frames from the input file depending on the format (H.265 vs AV1).
    // 1. We use a std::unique_ptr initialized outside the block to manage the parser's lifecycle.
    // 2. This prevents the parser (and its internal buffer) from being destroyed when the if-block ends.
    // 3. Ensuring the memory remains valid prevents a Segmentation Fault during the later transmission loop.
    std::unique_ptr<moq::app::H265FileParser> h265_parser;
    std::unique_ptr<moq::app::Av1IvfParser> ivf_parser;

    if (is_ivf) {
        ivf_parser = std::make_unique<moq::app::Av1IvfParser>(input_path);
        if (!ivf_parser->open()) {
            std::cerr << "Failed to open or parse IVF input: " << input_path << "\n";
            return 1;
        }
        av1_frames = ivf_parser->get_frames();
        size_t total_parsed_bytes = 0;
        for (const auto& f : av1_frames) total_parsed_bytes += f.size;
        std::cout << "Successfully parsed IVF file. Total Frames: " << av1_frames.size() << ", Total Bytes: " << total_parsed_bytes << "\n";
    } else {
        h265_parser = std::make_unique<moq::app::H265FileParser>(input_path);
        if (!h265_parser->open()) {
            std::cerr << "Failed to open or parse H.265 input: " << input_path << "\n";
            return 1;
        }
        h265_nalus = h265_parser->get_nalus();
        std::cout << "Successfully parsed H.265 file. Total NALUs: " << h265_nalus.size() << "\n";
    }

    std::cout << "Using max payload chunk size:     " << max_payload_size << " bytes\n";
    std::cout << "Configured Minimum MTU:           " << (min_mtu > 0 ? std::to_string(min_mtu) : "Default (PMTUD)") << "\n";
    std::cout << "Configured Maximum MTU:           " << (max_mtu > 0 ? std::to_string(max_mtu) : "Default (PMTUD)") << "\n";

    moq::transport::QuicConfig cfg;
    cfg.host = host;
    cfg.port = port;
    cfg.num_streams = num_streams;
    cfg.min_mtu = min_mtu;
    cfg.max_mtu = max_mtu;

    moq::transport::MsQuicTransport quic;

    if (!quic.start_client(cfg)) return 1;
    if (!quic.wait_connected()) return 2;
    if (!quic.wait_stream_ready()) return 3;

    // Set priority: Stream 0 is high priority (0xFFFF), others are low priority (0x0001)
    if (num_streams >= 2) {
        std::cout << "Configuring QUIC stream priorities...\n";
        quic.set_stream_priority(0, 0xFFFF); // Highest priority
        for (uint32_t s = 1; s < num_streams; ++s) {
            quic.set_stream_priority(s, 0x0001); // Lowest priority
        }
    }

    /* old code
    quic.set_send_expected(N);
    std::this_thread::sleep_for(std::chrono::milliseconds(200));
    for (int i = 0; i < N; ++i) {

        moq::PacketHeader hdr{};
        hdr.nalu_id = i;
        hdr.payload_len = 1024;
        std::vector<uint8_t> payload(1024, (uint8_t)i);

        // if (i == N - 1)
        //     quic.set_next_send_fin(true);

        if (!quic.send(0, hdr, payload.data()))
            return 4;
        std::this_thread::sleep_for(std::chrono::milliseconds(1000));  // 
    }
    */

    // 2. set payload size and use Packetizer
    moq::app::DefaultPacketizer packetizer(max_payload_size);

    uint32_t total_packets = 0;
    if (is_ivf) {
        for (const auto& frame : av1_frames) {
            total_packets += static_cast<uint32_t>(std::ceil(static_cast<double>(frame.size) / max_payload_size));
            if (frame.size == 0) total_packets++; 
        }
    } else {
        for (const auto& nalu : h265_nalus) {
            total_packets += static_cast<uint32_t>(std::ceil(static_cast<double>(nalu.size) / max_payload_size));
            if (nalu.size == 0) total_packets++; 
        }
    }

    // set expected total packets
    quic.set_send_expected(total_packets);
    std::this_thread::sleep_for(std::chrono::milliseconds(200));
    
    std::cout << "Start sending true media packets over QUIC (Packetized)...\n";

    // 3. send every packet
    uint32_t actual_packets_sent = 0;
    
    if (is_ivf) {
        for (size_t i = 0; i < av1_frames.size(); ++i) {
            uint32_t stream_idx = 0;
            if (num_streams >= 2) {
                if (av1_frames[i].is_important() || i == 0) { // First frame is likely Sequence Header
                    stream_idx = 0;
                } else {
                    stream_idx = 1; 
                    if (num_streams > 2) {
                        stream_idx = 1 + (i % (num_streams - 1));
                    }
                }
            }

            auto packets = packetizer.packetize(av1_frames[i], static_cast<uint32_t>(i));
            for (const auto& pkt : packets) {
                if (!quic.send(stream_idx, pkt.hdr, pkt.payload.data())) {
                    std::cerr << "Fail to send IVF Frame " << i << " Fragment " << pkt.hdr.fragment_idx << "\n";
                    return 4;
                }
                actual_packets_sent++;
            }
            if (pacing_delay_ms > 0) {
                std::this_thread::sleep_for(std::chrono::milliseconds(pacing_delay_ms));
            }
        }
    } else {
        for (size_t i = 0; i < h265_nalus.size(); ++i) {
            // split: important frame (I-frame/VPS/SPS/PPS) to Stream 0, others to Stream 1+
            uint32_t stream_idx = 0;
            if (num_streams >= 2) {
                if (h265_nalus[i].is_important()) {
                    stream_idx = 0;
                } else {
                    // split secondary packets to remaining streams (1 ~ num_streams-1)
                    stream_idx = 1; 
                    if (num_streams > 2) {
                        stream_idx = 1 + (i % (num_streams - 1));
                    }
                }
            }

            // split packets
            auto packets = packetizer.packetize(h265_nalus[i], static_cast<uint32_t>(i));
            
            for (const auto& pkt : packets) {
                if (!quic.send(stream_idx, pkt.hdr, pkt.payload.data())) {
                    std::cerr << "Fail to send NALU " << i << " Fragment " << pkt.hdr.fragment_idx << "\n";
                    return 4;
                }
                actual_packets_sent++;
            }
            if (pacing_delay_ms > 0) {
                std::this_thread::sleep_for(std::chrono::milliseconds(pacing_delay_ms));
            }
        }
    }

    std::string msg = "\n>>> [moq_client] " + std::to_string(actual_packets_sent) + 
                      " Application-Layer packets pushed to MSQuic.\n\n";
    quic.set_custom_shutdown_message(msg);

    if (!quic.wait_all_sends_done(30000)) { // original 10000, slightly longer for real video transmission time
        std::cerr << "Send not finished\n";
        return 5;
    }


    // close
    quic.disconnect();
    quic.wait_stream_shutdown(10000);

    std::cerr << "Client finished cleanly. Waiting 2 seconds for final server ACKs/CLOSE packets...\n";
    std::this_thread::sleep_for(std::chrono::seconds(2));
    return 0;
}
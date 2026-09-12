#include "lib/client_bridge.h"
// #include "lib/nat_header.h" // [REMOVED NatHeader]
#include "ns3/core-module.h"
#include "ns3/network-module.h"
#include "ns3/internet-module.h"

#include <sys/socket.h>
#include <netinet/in.h>
#include <arpa/inet.h>
#include <unistd.h>
#include <iostream>

using namespace ns3;

ClientBridgeApp::ClientBridgeApp() : running_(false), real_fd_(-1), has_client_addr_(false), tx_packets_(0), rx_packets_(0) {}

ClientBridgeApp::~ClientBridgeApp() {
    running_ = false;
    if (real_fd_ >= 0) {
        shutdown(real_fd_, SHUT_RDWR); 
        close(real_fd_);
        real_fd_ = -1;
    }
    if (rx_thread_.joinable()) {
        rx_thread_.join();
    }
}

// void ClientBridgeApp::Setup(uint16_t listen_port, ns3::Ipv4Address peerAddress, uint16_t peerPort) {
void ClientBridgeApp::Setup(uint16_t listen_port, uint16_t my_virtual_port, ns3::Ipv4Address peerAddress, uint16_t peerPort) {
    // Set local real listening port and ns-3 peer IP/Port
    my_listen_port_ = listen_port;
    my_virtual_port_ = my_virtual_port; // [ADDED] Store virtual listening port
    peer_addr_ = peerAddress;
    peer_port_ = peerPort;
}

void ClientBridgeApp::StartApplication() {
    // Create ns-3 internal UDP Socket and connect to the peer (ServerBridge)
    ns3_socket_ = ns3::Socket::CreateSocket(GetNode(), ns3::UdpSocketFactory::GetTypeId());
    
    // [ADDED] Explicitly bind to my specific virtual port (e.g. 1000)
    ns3_socket_->Bind(ns3::InetSocketAddress(ns3::Ipv4Address::GetAny(), my_virtual_port_));
    
    ns3_socket_->Connect(ns3::InetSocketAddress(peer_addr_, peer_port_));
    ns3_socket_->SetRecvCallback(ns3::MakeCallback(&ClientBridgeApp::ReceiveFromNs3, this));

    // Create a real-world UDP socket to receive packets from physical client apps (e.g., QUIC Client)
    real_fd_ = socket(AF_INET, SOCK_DGRAM, 0);
    struct sockaddr_in my_addr;
    my_addr.sin_family = AF_INET;
    my_addr.sin_port = htons(my_listen_port_);
    my_addr.sin_addr.s_addr = INADDR_ANY;
    bind(real_fd_, (struct sockaddr *)&my_addr, sizeof(my_addr));

    // [ADDED] Increase socket receive buffer to prevent packet loss in Linux kernel
    int rcvbuf = 1024 * 1024 * 50; // 50 MB
    setsockopt(real_fd_, SOL_SOCKET, SO_RCVBUF, &rcvbuf, sizeof(rcvbuf));

    running_ = true;
    // Start a dedicated thread to process packets received from the real world
    rx_thread_ = std::thread(&ClientBridgeApp::PhysicalRecvLoop, this);
}

void ClientBridgeApp::StopApplication() {
    running_ = false;
    if (real_fd_ >= 0) {
        shutdown(real_fd_, SHUT_RDWR);
        close(real_fd_);
        real_fd_ = -1;
    }
    if (rx_thread_.joinable()) {
        rx_thread_.join();
    }
    // [ADDED] Print packet statistics
    std::cout << "[ClientBridge " << my_listen_port_ << "] Packets entered ns3: " << tx_packets_ 
              << " | Packets left ns3: " << rx_packets_ << std::endl;
}

void ClientBridgeApp::PhysicalRecvLoop() {
    uint8_t buffer[2048];
    struct sockaddr_in client_addr;
    socklen_t addr_len = sizeof(client_addr);

    while (running_) {
        // Receive packet from the real client
        ssize_t bytes = recvfrom(real_fd_, buffer, sizeof(buffer), 0, (struct sockaddr*)&client_addr, &addr_len);
        if (bytes > 0) {
            tx_packets_++; // [ADDED] Increment enter ns-3 packet count
            if (tx_packets_ % 500 == 0) {
                std::cout << "[ClientBridge " << my_listen_port_ << "] Realtime - Tx: " << tx_packets_ 
                          << " | Rx: " << rx_packets_ << std::endl;
            }

            // [ADDED] Store the physical client address for routing return packets without NatHeader
            client_addr_ = client_addr;
            has_client_addr_ = true;

            std::vector<uint8_t> bridged_data;
            
            /* [REMOVED NatHeader]
            // Create a NatHeader to record the real client's IP and Port
            NatHeader hdr;
            hdr.real_ip = client_addr.sin_addr.s_addr;
            hdr.real_port = client_addr.sin_port;
                
            // Prepend the NatHeader to the original payload
            uint8_t* hdr_ptr = reinterpret_cast<uint8_t*>(&hdr);
            bridged_data.insert(bridged_data.end(), hdr_ptr, hdr_ptr + sizeof(NatHeader));
            */

            // [MODIFIED] Insert only the pure buffer payload
            bridged_data.insert(bridged_data.end(), buffer, buffer + bytes);

            // Schedule the packet injection into the ns-3 simulation environment (thread-safe)
            ns3::Simulator::ScheduleWithContext(
                GetNode()->GetId(), ns3::Seconds(0), 
                &ClientBridgeApp::InjectToNs3, this, bridged_data);
        }
    }
}

void ClientBridgeApp::InjectToNs3(std::vector<uint8_t> data) {
    // Create an ns-3 packet and send it (destination is set during Connect)
    ns3::Ptr<ns3::Packet> p = ns3::Create<ns3::Packet>(data.data(), data.size());
    ns3_socket_->Send(p);
}

void ClientBridgeApp::ReceiveFromNs3(ns3::Ptr<ns3::Socket> socket) {
    ns3::Ptr<ns3::Packet> packet;
    ns3::Address from;
    while ((packet = socket->RecvFrom(from))) {
        if (!has_client_addr_) {
            continue; // No physical client address known yet
        }

        rx_packets_++; // [ADDED] Increment leave ns-3 packet count
        if (rx_packets_ % 1000 == 0){
            std::cout << "[ClientBridge " << my_listen_port_ << "] Realtime - Tx: " << tx_packets_ 
                      << " | Rx: " << rx_packets_ << std::endl;
        }

        // Receive packet returned from ns-3 (ServerBridge)
        uint8_t buffer[2048];
        size_t packet_size = packet->GetSize();
        if (packet_size > sizeof(buffer)) {
            packet_size = sizeof(buffer);
        }
        packet->CopyData(buffer, packet_size);
    
        /* [REMOVED NatHeader]
        // Parse the NatHeader to get the original sending client's real IP and Port
        NatHeader* hdr = reinterpret_cast<NatHeader*>(buffer);
    
        struct sockaddr_in client_addr;
        client_addr.sin_family = AF_INET;
        client_addr.sin_addr.s_addr = hdr->real_ip;
        client_addr.sin_port = hdr->real_port;
    
        // Calculate and extract the real payload (excluding NatHeader)
        size_t payload_len = packet->GetSize() - sizeof(NatHeader);
        uint8_t* payload_ptr = buffer + sizeof(NatHeader);
            
        // Send the response back to the real client via the physical socket
        sendto(real_fd_, payload_ptr, payload_len, 0, 
               (struct sockaddr*)&client_addr, sizeof(client_addr));
        */

        // [MODIFIED] Send the raw payload directly to the stored client_addr_
        sendto(real_fd_, buffer, packet_size, 0, 
               (struct sockaddr*)&client_addr_, sizeof(client_addr_));
    }
}

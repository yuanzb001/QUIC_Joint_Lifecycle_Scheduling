#include "lib/server_bridge.h"
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

ServerBridgeApp::ServerBridgeApp() : running_(false), tx_packets_(0), rx_packets_(0) {}

ServerBridgeApp::~ServerBridgeApp() {
    running_ = false;
    std::lock_guard<std::mutex> lock(nat_mutex_);
    for (auto const& pair : nat_table_) {
        shutdown(pair.second, SHUT_RDWR);
        close(pair.second);
    }
    nat_table_.clear();
}

void ServerBridgeApp::Setup(uint16_t virtual_port, uint16_t target_server_port) {
    // Set ns-3 internal virtual listening port and the target real server port to forward to
    my_virtual_port_ = virtual_port;
    target_server_port_ = target_server_port;
}

void ServerBridgeApp::StartApplication() {
    // Create ns-3 internal UDP Socket and bind to the virtual port
    ns3_socket_ = ns3::Socket::CreateSocket(GetNode(), ns3::UdpSocketFactory::GetTypeId());
    ns3_socket_->Bind(ns3::InetSocketAddress(ns3::Ipv4Address::GetAny(), my_virtual_port_));
    // Set callback to receive packets from ns-3
    ns3_socket_->SetRecvCallback(ns3::MakeCallback(&ServerBridgeApp::ReceiveFromNs3, this));
        
    running_ = true;
}

void ServerBridgeApp::StopApplication() {
    running_ = false;
    std::lock_guard<std::mutex> lock(nat_mutex_);
    for (auto const& pair : nat_table_) {
        shutdown(pair.second, SHUT_RDWR);
        close(pair.second);
    }
    nat_table_.clear();
    // [ADDED] Print packet statistics
    std::cout << "[ServerBridge " << target_server_port_ << "] Packets entered ns3: " << tx_packets_ 
              << " | Packets left ns3: " << rx_packets_ << std::endl;
}

void ServerBridgeApp::ReceiveFromNs3(ns3::Ptr<ns3::Socket> socket) {
    ns3::Ptr<ns3::Packet> packet;
    ns3::Address from;
    while ((packet = socket->RecvFrom(from))) {
        rx_packets_++; // [ADDED] Increment leave ns-3 packet count
        if (rx_packets_ % 500 == 0) {
            std::cout << "[ServerBridge " << target_server_port_ << "] Realtime - Tx: " << tx_packets_ 
                      << " | Rx: " << rx_packets_ << std::endl;
        }

        // Receive packet from ns-3 and copy data to buffer
        uint8_t buffer[2048];
        size_t packet_size = packet->GetSize();
        if (packet_size > sizeof(buffer)) {
            packet_size = sizeof(buffer);
        }
        packet->CopyData(buffer, packet_size);

        /* [REMOVED NatHeader]
        // Parse the prepended NatHeader (contains the real Client's IP and Port)
        NatHeader* hdr = reinterpret_cast<NatHeader*>(buffer);
        uint64_t client_key = ((uint64_t)hdr->real_ip << 32) | hdr->real_port;
        */

        // [MODIFIED] Use ns-3 simulated origin address from 'from' to derive client_key
        ns3::InetSocketAddress inet_addr = ns3::InetSocketAddress::ConvertFrom(from);
        uint32_t sim_ip = inet_addr.GetIpv4().Get();
        uint16_t sim_port = inet_addr.GetPort();
        uint64_t client_key = ((uint64_t)sim_ip << 32) | sim_port;
            
        int server_facing_fd = -1;
        {
            std::lock_guard<std::mutex> lock(nat_mutex_);
            // If this is the first time receiving connection from this Client, create a new physical Socket (NAT mechanism)
            if (nat_table_.find(client_key) == nat_table_.end()) {
                server_facing_fd = ::socket(AF_INET, SOCK_DGRAM, 0);
                nat_table_[client_key] = server_facing_fd;
                
                /* [REMOVED NatHeader]
                // Start a thread specifically to listen to this Socket for the real Server's replies
                std::thread(&ServerBridgeApp::ServerRecvLoop, this, server_facing_fd, *hdr, from).detach();
                */

                // [MODIFIED] Start thread without NatHeader
                std::thread(&ServerBridgeApp::ServerRecvLoop, this, server_facing_fd, from).detach();
            } else {
                server_facing_fd = nat_table_[client_key];
            }
        }

        // Set the destination address of the real server (localhost)
        struct sockaddr_in srv_addr;
        srv_addr.sin_family = AF_INET;
        srv_addr.sin_port = htons(target_server_port_);
    // [ADDED] Increase socket receive buffer to prevent packet loss in Linux kernel
    int rcvbuf = 1024 * 1024 * 50; // 50 MB
    setsockopt(server_facing_fd, SOL_SOCKET, SO_RCVBUF, &rcvbuf, sizeof(rcvbuf));
        inet_pton(AF_INET, "127.0.0.1", &srv_addr.sin_addr);
            
        /* [REMOVED NatHeader]
        // After removing NatHeader, send the original payload to the real server
        sendto(server_facing_fd, buffer + sizeof(NatHeader), packet->GetSize() - sizeof(NatHeader), 
               0, (struct sockaddr*)&srv_addr, sizeof(srv_addr));
        */

        // [MODIFIED] Send the raw payload to the real server directly
        sendto(server_facing_fd, buffer, packet_size, 
               0, (struct sockaddr*)&srv_addr, sizeof(srv_addr));
    }
}

// void ServerBridgeApp::ServerRecvLoop(int fd, NatHeader client_hdr, ns3::Address tx_bridge_addr) {
void ServerBridgeApp::ServerRecvLoop(int fd, ns3::Address tx_bridge_addr) {
    uint8_t buffer[2048];
    while (running_) {
        // Receive response from the real Server
        ssize_t bytes = recv(fd, buffer, sizeof(buffer), 0);
        if (bytes > 0) {
            tx_packets_++; // [ADDED] Increment enter ns-3 packet count
            if (tx_packets_ % 500 == 0 || tx_packets_ > 7000) {
                std::cout << "[ServerBridge " << target_server_port_ << "] Realtime - Tx: " << tx_packets_ 
                          << " | Rx: " << rx_packets_ << std::endl;
            }

            std::vector<uint8_t> bridged_data;
            
            /* [REMOVED NatHeader]
            // Prepend the original Client's NatHeader so ClientBridge knows where to forward
            uint8_t* hdr_ptr = reinterpret_cast<uint8_t*>(&client_hdr);
            bridged_data.insert(bridged_data.end(), hdr_ptr, hdr_ptr + sizeof(NatHeader));
            */

            // [MODIFIED] Insert pure buffer payload
            bridged_data.insert(bridged_data.end(), buffer, buffer + bytes);

            // Schedule the packet to be injected back into the ns-3 environment (via ns-3 simulator context for thread safety)
            ns3::Simulator::ScheduleWithContext(
                GetNode()->GetId(), ns3::Seconds(0), 
                &ServerBridgeApp::InjectToNs3, this, bridged_data, tx_bridge_addr);
        } else {
            break; 
        }
    }
}

void ServerBridgeApp::InjectToNs3(std::vector<uint8_t> data, ns3::Address dest) {
    // Create an ns-3 packet and send it back to ClientBridge via the ns-3 internal Socket
    ns3::Ptr<ns3::Packet> p = ns3::Create<ns3::Packet>(data.data(), data.size());
    ns3_socket_->SendTo(p, 0, dest);
}

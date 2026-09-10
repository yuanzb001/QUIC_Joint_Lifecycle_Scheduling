#pragma once
#include "ns3/application.h"
#include "ns3/socket.h"
#include <thread>
#include <atomic>
#include <map>
#include <mutex>
#include <vector>

// Forward declaration to avoid unnecessary headers
// struct NatHeader; // [REMOVED NatHeader]

class ServerBridgeApp : public ns3::Application {
public:
    ServerBridgeApp();
    virtual ~ServerBridgeApp();

    void Setup(uint16_t virtual_port, uint16_t target_server_port);

protected:
    virtual void StartApplication(void) override;
    virtual void StopApplication(void) override;

private:
    ns3::Ptr<ns3::Socket> ns3_socket_;
    std::atomic<bool> running_;
    
    std::map<uint64_t, int> nat_table_;
    std::mutex nat_mutex_;

    uint16_t my_virtual_port_;
    uint16_t target_server_port_;

    std::atomic<uint64_t> tx_packets_; // [ADDED] Packets entering ns-3 (from real Server)
    std::atomic<uint64_t> rx_packets_; // [ADDED] Packets leaving ns-3 (to real Server)

    void ReceiveFromNs3(ns3::Ptr<ns3::Socket> socket);
    // void ServerRecvLoop(int fd, NatHeader client_hdr, ns3::Address tx_bridge_addr); // [REMOVED NatHeader]
    void ServerRecvLoop(int fd, ns3::Address tx_bridge_addr);
    void InjectToNs3(std::vector<uint8_t> data, ns3::Address dest);
};

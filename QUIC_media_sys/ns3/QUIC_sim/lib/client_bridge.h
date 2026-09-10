#pragma once
#include "ns3/application.h"
#include "ns3/socket.h"
#include "ns3/ipv4-address.h"
#include <thread>
#include <atomic>
#include <vector>
#include <netinet/in.h>

class ClientBridgeApp : public ns3::Application {
public:
    ClientBridgeApp();
    virtual ~ClientBridgeApp();

    // [MODIFIED] Added my_virtual_port parameter
    // void Setup(uint16_t listen_port, ns3::Ipv4Address peerAddress, uint16_t peerPort);
    void Setup(uint16_t listen_port, uint16_t my_virtual_port, ns3::Ipv4Address peerAddress, uint16_t peerPort);

protected:
    virtual void StartApplication(void) override;
    virtual void StopApplication(void) override;

private:
    std::thread rx_thread_;
    std::atomic<bool> running_;
    int real_fd_;
    ns3::Ptr<ns3::Socket> ns3_socket_;
    ns3::Ipv4Address peer_addr_;
    uint16_t peer_port_;
    uint16_t my_listen_port_;
    uint16_t my_virtual_port_; // [ADDED] Client Bridge's virtual port in ns-3

    // [ADDED] Track real client address for forwarding return packets without NatHeader
    struct sockaddr_in client_addr_;
    std::atomic<bool> has_client_addr_;

    std::atomic<uint64_t> tx_packets_; // [ADDED] Packets entering ns-3
    std::atomic<uint64_t> rx_packets_; // [ADDED] Packets leaving ns-3

    void PhysicalRecvLoop();
    void InjectToNs3(std::vector<uint8_t> data);
    void ReceiveFromNs3(ns3::Ptr<ns3::Socket> socket);
};

// 1. Include ns-3 and standard library headers
#include "ns3/core-module.h"
#include "ns3/network-module.h"
#include "ns3/internet-module.h"
#include "ns3/point-to-point-module.h"
#include "ns3/applications-module.h"
#include "ns3/flow-monitor-module.h"
#include <csignal>
#include <cstdlib>
#include <fstream>
#include <vector>
#include <string>
#include <chrono>

#include "lib/client_bridge.h"
#include "lib/server_bridge.h"

using namespace ns3;

static Ptr<FlowMonitor> g_monitor = nullptr;
static FlowMonitorHelper* g_flowmon_ptr = nullptr;
static uint32_t g_startIndex = 0;

// ============================================
// Trace Parsing & Dynamic Bandwidth Logic
// ============================================

struct TraceEntry {
    double time;
    double bandwidth;
};

// Load the trace file into a vector of TraceEntry
std::vector<TraceEntry> LoadTrace(const std::string& filename) {
    std::vector<TraceEntry> trace;
    std::ifstream infile(filename);
    if (!infile.is_open()) {
        std::cerr << "Could not open trace file: " << filename << std::endl;
        std::exit(1);
    }
    double time, bw;
    while (infile >> time >> bw) {
        trace.push_back({time, bw});
    }
    return trace;
}

// Scheduled function to change bandwidth dynamically
void ChangeBandwidth(Ptr<NetDevice> dev1, Ptr<NetDevice> dev2, std::vector<TraceEntry> trace, uint32_t index, std::string unit) {
    if (trace.empty()) return;

    double bw = trace[index].bandwidth;
    
    // Protection against division by zero in ns-3
    if (bw <= 0.000001) {
        bw = 0.001; 
    }
    
    std::string dataRate = std::to_string(bw) + unit;
    
    Ptr<PointToPointNetDevice> p2pDev1 = DynamicCast<PointToPointNetDevice>(dev1);
    Ptr<PointToPointNetDevice> p2pDev2 = DynamicCast<PointToPointNetDevice>(dev2);
    
    // Update the DataRate of the PointToPoint links
    if (p2pDev1) p2pDev1->SetAttribute("DataRate", StringValue(dataRate));
    if (p2pDev2) p2pDev2->SetAttribute("DataRate", StringValue(dataRate));

    std::cout << "[Trace] Time " << Simulator::Now().GetSeconds() << "s: Link Bandwidth changed to " << dataRate << " (Trace index: " << index << ")" << std::endl;

    // Calculate time until next change
    uint32_t nextIndex = (index + 1) % trace.size();
    double interval = 0;
    
    // Handle closed-loop
    if (nextIndex == 0) {
        if (trace.size() > 1) {
             interval = trace[1].time - trace[0].time; // assume regular interval based on first two elements
        } else {
             interval = 1.0;
        }
    } else {
        interval = trace[nextIndex].time - trace[index].time;
    }

    if (interval <= 0) interval = 1.0; // Fallback for safety

    // Schedule next change recursively
    Simulator::Schedule(Seconds(interval), &ChangeBandwidth, dev1, dev2, trace, nextIndex, unit);
}

void InterruptHandler(int signum) {
    if (signum == SIGTSTP) {
        std::cout << "\n\n[ns-3] SIGTSTP (Ctrl+Z) received. Instantly generating simulation statistics and exiting..." << std::endl;
    } else {
        std::cout << "\n\n[ns-3] SIGINT (Ctrl+C) received. Instantly generating simulation statistics and exiting..." << std::endl;
    }

    if (g_monitor && g_flowmon_ptr) {
        g_monitor->CheckForLostPackets();
        Ptr<Ipv4FlowClassifier> classifier =
            DynamicCast<Ipv4FlowClassifier>(g_flowmon_ptr->GetClassifier());
        std::map<FlowId, FlowMonitor::FlowStats> stats = g_monitor->GetFlowStats();

        for (auto const &flow : stats)
        {
            std::cout << "Flow ID: " << flow.first << std::endl;
            std::cout << "  Tx Packets: " << flow.second.txPackets << std::endl;
            std::cout << "  Rx Packets: " << flow.second.rxPackets << std::endl;
            std::cout << "  Lost Packets: "
                      << flow.second.txPackets - flow.second.rxPackets << std::endl;

            if (flow.second.rxPackets > 0) {
                double throughput =
                    flow.second.rxBytes * 8.0 /
                    (flow.second.timeLastRxPacket.GetSeconds() - flow.second.timeFirstTxPacket.GetSeconds()) / 1e6;

                std::cout << "  Throughput: " << throughput << " Mbps\n";
                std::cout << "  Mean Delay: "
                          << (flow.second.delaySum.GetSeconds() / flow.second.rxPackets) << " s\n";
            } else {
                std::cout << "  Throughput: 0 Mbps\n";
                std::cout << "  Mean Delay: N/A (no received packets)\n";
            }

            std::cout << "-----------------------------" << std::endl;
        }

        g_monitor->SerializeToXmlFile("flowmon.xml", true, true);
    }

    Simulator::Destroy();
    std::exit(0);
}

int main(int argc, char *argv[])
{
    // Register SIGTSTP (Ctrl+Z) and SIGINT (Ctrl+C) handlers to instantly display statistics and exit
    signal(SIGTSTP, InterruptHandler);
    signal(SIGINT, InterruptHandler);

    // [Key 1] Force RealtimeSimulatorImpl, otherwise physical sockets might timeout
    GlobalValue::Bind("SimulatorImplementationType", StringValue("ns3::RealtimeSimulatorImpl"));

    double errorRate = 0.0;
    std::string dataRate = "10Mbps";
    std::string delay = "5ms";
    uint16_t numClients = 3; // By default, open 3 connection pairs
    
    // Trace parameters
    std::string traceFile = "";
    std::string traceUnit = "Mbps";
    bool randomStart = true;
    double simTime = 200.0;

    CommandLine cmd;
    cmd.AddValue("errorRate", "Packet loss rate", errorRate);
    cmd.AddValue("dataRate", "Link bandwidth (initial)", dataRate);
    cmd.AddValue("delay", "Link delay", delay);
    cmd.AddValue("numClients", "Client count", numClients);
    cmd.AddValue("traceFile", "Path to bandwidth trace file", traceFile);
    cmd.AddValue("traceUnit", "Unit for bandwidth in trace file (e.g., Mbps, Kbps)", traceUnit);
    cmd.AddValue("randomStart", "Randomly choose start point in trace", randomStart);
    cmd.AddValue("simTime", "Total simulation time (seconds)", simTime);
    cmd.Parse(argc, argv);

    NodeContainer hosts;
    hosts.Create(2);

    NodeContainer routers;
    routers.Create(2);

    InternetStackHelper stack;
    stack.Install(hosts);
    stack.Install(routers);

    PointToPointHelper p2p_edge;
    p2p_edge.SetDeviceAttribute("DataRate", StringValue("10Gbps"));
    p2p_edge.SetChannelAttribute("Delay", StringValue("1ms"));

    PointToPointHelper p2p_bottleneck;
    p2p_bottleneck.SetDeviceAttribute("DataRate", StringValue(dataRate));
    p2p_bottleneck.SetChannelAttribute("Delay", StringValue(delay));

    // Create network topology: Host0 <--> Router0 <--> Router1 <--> Host1
    NetDeviceContainer d1 = p2p_edge.Install(hosts.Get(0), routers.Get(0));
    NetDeviceContainer d2 = p2p_bottleneck.Install(routers.Get(0), routers.Get(1)); // Bottleneck link
    NetDeviceContainer d3 = p2p_edge.Install(routers.Get(1), hosts.Get(1));

    // Enable PCAP for Wireshark analysis
    p2p_bottleneck.EnablePcapAll("quic-sim");

    // Setup error model between Router0 and Router1 (simulating packet loss)
    Ptr<RateErrorModel> em = CreateObject<RateErrorModel>();
    em->SetAttribute("ErrorUnit", EnumValue(RateErrorModel::ERROR_UNIT_PACKET)); // Drop by packet
    em->SetAttribute("ErrorRate", DoubleValue(errorRate));
    d2.Get(1)->SetAttribute("ReceiveErrorModel", PointerValue(em));

    Ipv4AddressHelper address;
    // Assign IP: Subnet 10.0.1.0 (Host0 - Router0)
    address.SetBase("10.0.1.0", "255.255.255.0");
    Ipv4InterfaceContainer i1 = address.Assign(d1);

    // Assign IP: Subnet 10.0.2.0 (Router0 - Router1)
    address.SetBase("10.0.2.0", "255.255.255.0");
    Ipv4InterfaceContainer i2 = address.Assign(d2);

    // Assign IP: Subnet 10.0.3.0 (Router1 - Host1)
    address.SetBase("10.0.3.0", "255.255.255.0");
    Ipv4InterfaceContainer i3 = address.Assign(d3);

    // Populate global routing tables so all nodes can communicate
    Ipv4GlobalRoutingHelper::PopulateRoutingTables();

    // ============================================
    // Schedule Trace Bandwidth
    // ============================================
    if (!traceFile.empty()) {
        std::vector<TraceEntry> trace = LoadTrace(traceFile);
        if (!trace.empty()) {
            uint32_t startIndex = 0;
            if (randomStart) {
                Ptr<UniformRandomVariable> uv = CreateObject<UniformRandomVariable>();
                uv->SetAttribute("Min", DoubleValue(0));
                uv->SetAttribute("Max", DoubleValue(trace.size() - 1));
                g_startIndex = uv->GetInteger();
                startIndex = g_startIndex;
                std::cout << "[Trace] Loaded " << trace.size() << " entries. Random start index: " << startIndex << std::endl;
            } else {
                std::cout << "[Trace] Loaded " << trace.size() << " entries. Starting at index 0." << std::endl;
            }
            
            // Schedule the first bandwidth change at time 0.0
            Simulator::Schedule(Seconds(0.0), &ChangeBandwidth, d2.Get(0), d2.Get(1), trace, startIndex, traceUnit);
        }
    } else {
        std::cout << "[Trace] No trace file specified. Using static bandwidth: " << dataRate << std::endl;
    }

    // ============================================
    // Dynamically instantiate bridges based on numClients
    // ============================================
    for (uint16_t i = 0; i < numClients; ++i) {
        // Calculate dedicated ports using index i
        uint16_t client_port = 8000 + i;       // Client entry port (8000, 8001, 8002...)
        uint16_t client_virtual_port = 1000 + i; // ClientBridge in ns-3 listens on 1000, 1001...
        uint16_t server_virtual_port = 9999 + i; // ServerBridge in ns-3 listens on 9999, 10000...
        uint16_t server_port = 4433 + i;   // Real Server listening port (4433, 4434, 4435...)

        // Install ServerBridge on Host1 (H2)
        Ptr<ServerBridgeApp> serverApp = CreateObject<ServerBridgeApp>();
        serverApp->Setup(server_virtual_port, server_port);
        hosts.Get(1)->AddApplication(serverApp);
        serverApp->SetStartTime(Seconds(1.0));
        serverApp->SetStopTime(Seconds(simTime + 10.0));

        // Install ClientBridge on Host0 (H1)
        Ptr<ClientBridgeApp> clientApp = CreateObject<ClientBridgeApp>();
        clientApp->Setup(client_port, client_virtual_port, i3.GetAddress(1), server_virtual_port);
        hosts.Get(0)->AddApplication(clientApp);
        clientApp->SetStartTime(Seconds(1.0));
        clientApp->SetStopTime(Seconds(simTime + 10.0));
    }

    FlowMonitorHelper flowmon;
    Ptr<FlowMonitor> monitor = flowmon.InstallAll();
    g_flowmon_ptr = &flowmon;
    g_monitor = monitor;

    // Write start time for python client sync
    {
        auto now = std::chrono::system_clock::now();
        double epoch_time = std::chrono::duration_cast<std::chrono::microseconds>(now.time_since_epoch()).count() / 1000000.0;
        std::ofstream sync_file("/tmp/ns3_sync.txt");
        sync_file << std::fixed << epoch_time << "\n";
        sync_file << g_startIndex << "\n";
    }

    Simulator::Stop(Seconds(simTime));
    Simulator::Run();

    monitor->CheckForLostPackets();
    Ptr<Ipv4FlowClassifier> classifier =
        DynamicCast<Ipv4FlowClassifier>(flowmon.GetClassifier());
    std::map<FlowId, FlowMonitor::FlowStats> stats = monitor->GetFlowStats();

    for (auto const &flow : stats)
    {
        std::cout << "Flow ID: " << flow.first << std::endl;
        std::cout << "  Tx Packets: " << flow.second.txPackets << std::endl;
        std::cout << "  Rx Packets: " << flow.second.rxPackets << std::endl;
        std::cout << "  Lost Packets: "
                  << flow.second.txPackets - flow.second.rxPackets << std::endl;

       if (flow.second.rxPackets > 0) {
            double throughput =
                flow.second.rxBytes * 8.0 /
                (flow.second.timeLastRxPacket.GetSeconds() - flow.second.timeFirstTxPacket.GetSeconds()) / 1e6;

            std::cout << "  Throughput: " << throughput << " Mbps\n";
            std::cout << "  Mean Delay: "
                      << (flow.second.delaySum.GetSeconds() / flow.second.rxPackets) << " s\n";
        } else {
            std::cout << "  Throughput: 0 Mbps\n";
            std::cout << "  Mean Delay: N/A (no received packets)\n";
        }

                std::cout << "-----------------------------" << std::endl;
    }

    monitor->SerializeToXmlFile("flowmon.xml", true, true);

    Simulator::Destroy();
    return 0;

}

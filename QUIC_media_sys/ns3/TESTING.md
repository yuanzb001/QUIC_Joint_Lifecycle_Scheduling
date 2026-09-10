# ns-3 Network Simulation Testing Guide (Localhost Workflow)

This guide details how to seamlessly inject real-world `moq_client` and `moq_server` traffic into an ns-3 simulated network environment on a **single machine (localhost)**. This allows you to evaluate QUIC performance across challenging network conditions such as elevated latency, packet loss, and bandwidth bottlenecks.

---

> [!NOTE]
> For the comprehensive architectural topology diagram, port forwarding mechanisms, and underlying C++ bridge architecture (`ClientBridgeApp` / `ServerBridgeApp`), please refer to **[ns3/INSTALL.md](INSTALL.md)**.

---

## 🚀 Execution Steps

Please open separate terminal windows and follow the strict startup order: **Server ➔ Bridge ➔ Client**.

### Step 1: Start the Local Server (`moq_server`)
In Terminal A, start the server listening on the standard port `4433`:
```bash
cd build
./moq_server 0.0.0.0 4433 output_sim.h265 ../certs/server.crt ../certs/server.key --min_mtu=1300 --max_mtu=1400
```
*(Note: Keep MTU parameters symmetrical with the client configuration)*

### Step 2: Start the ns-3 Simulation Bridge (`Bridge`)
In Terminal B, navigate to the ns-3 root directory and launch the simulation script. This automatically binds OS port `8000` as the bridge entry point:
```bash
# Execute within the ns-3 root directory:
./ns3 run "QUIC_sim/two_router_udp --errorRate=0.01 --dataRate=10Mbps --delay=5ms --numClients=1"
```
- `--errorRate=0.01`: Configures a 1% packet loss rate.
- `--dataRate=10Mbps`: Configures the bottleneck link bandwidth.
- `--delay=5ms`: Configures link transmission latency.

### Step 3: Start the Local Client (`moq_client`)
In Terminal C, start the client. **IMPORTANT: Target the bridge entry port `8000` instead of port 4433**:
```bash
cd build
./moq_client 127.0.0.1 8000 input.h265 --streams=2 --chunksize=1200 --min_mtu=1300 --max_mtu=1400
```

---

## 📌 Important Notes & Troubleshooting

1. **Strict Startup Hierarchy**:
   Always launch `moq_server` first, followed by the ns-3 simulation (`Bridge`), and finally `moq_client`. If launched out of order, NAT mapping tables will fail to initialize and connection attempts will be rejected.

2. **Verify Port Availability**:
   Prior to launching the simulation, ensure ports `8000` and `4433` are free. If a background process is holding either port, terminate it using `pkill -f moq_server` or `pkill -f ns3`.

3. **Symmetrical MTU Settings**:
   Due to MsQuic PMTUD behaviors across simulated virtual bridge networks, ensure both client and server enforce explicit, matching `--min_mtu=N` and `--max_mtu=N` named parameters to avoid handshake stalls.

4. **UDP Buffer Tuning & App-Layer Pacing**:
   To avoid overwhelming the ns-3 bridge and triggering kernel-level UDP packet drops during high-speed video transfers, the `ClientBridgeApp` and `ServerBridgeApp` automatically configure a 50MB `SO_RCVBUF` for the OS sockets. Furthermore, use the `--delay=N` parameter on `moq_client` to enable app-layer packet pacing.

5. **ns-3 Simulation Telemetry Display Timing**:
   During active streaming, `moq_client` and `moq_server` log transmission events in real time. However, ns-3 bridge packet statistics (`tx_packets_` / `rx_packets_` and FlowMonitor metrics) **are only printed to the console once the simulation reaches its configured end time or when manually interrupted via `Ctrl + Z` or `Ctrl + C`**. It is normal for the console to appear quiet immediately after media transfer completes.

6. **Automatic Simulation Stop Time**:
   By default, the simulation is configured to automatically terminate after 120 seconds (`Simulator::Stop(Seconds(120.0));`). If you are testing larger media files or using significant pacing delays that cause the transmission to exceed 120 seconds, the simulation will cut off prematurely. You can increase this limit directly in the `ns3/QUIC_sim/two_router_udp.cc` script.

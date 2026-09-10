# ns-3 網路模擬測試指引 (Local 實測指南)

本指引介紹如何在 **單一機器 (Localhost)** 上，將真實世界的 `moq_client` 與 `moq_server` 傳輸流量無縫導入 ns-3 網路模擬環境，以評估在不同網路條件（如延遲、遺失率、頻寬限制）下的 QUIC 傳輸表現。

---

> [!NOTE]
> 關於 ns-3 模擬核心與橋接應用程式 (`ClientBridgeApp` / `ServerBridgeApp`) 的詳細系統拓撲圖、連接埠轉發機制與底層原始碼架構，請參閱 **[ns3/INSTALL_ZH.md](INSTALL_ZH.md)**。

---

## 🚀 完整執行步驟 (Localhost)

請依序開啟不同的終端機視窗，並確保啟動順序為：**Server ➔ Bridge ➔ Client**。

### 步驟 1：啟動本機伺服器 (`moq_server`)
開啟終端機 A，啟動伺服器端並監聽標準的 `4433` 埠號：
```bash
cd build
./moq_server 0.0.0.0 4433 output_sim.h265 ../certs/server.crt ../certs/server.key --min_mtu=1300 --max_mtu=1400
```
*(備註：為確保與 Client 設定一致，請同步指定 MTU 參數)*

### 步驟 2：啟動 ns-3 模擬器與橋接中繼 (`Bridge`)
開啟終端機 B，進入 ns-3 根目錄並執行模擬器腳本。這會自動在本機綁定 `8000` 連接埠作為入口：
```bash
# 位於 ns-3 根目錄下執行：
./ns3 run "QUIC_sim/two_router_udp --errorRate=0.01 --dataRate=10Mbps --delay=5ms --numClients=1"
```
- `--errorRate=0.01`：設定 1% 的封包遺失率。
- `--dataRate=10Mbps`：設定通道頻寬限制。
- `--delay=5ms`：設定通道傳輸延遲。

### 步驟 3：啟動本機用戶端 (`moq_client`)
開啟終端機 C，啟動 Client。**注意：此時目的地連接埠需設定為 Bridge 監聽的 `8000`，而非 4433**：
```bash
cd build
./moq_client 127.0.0.1 8000 input.h265 --streams=2 --chunksize=1200 --min_mtu=1300 --max_mtu=1400
```

---

## 📌 注意事項與避坑指南

1. **嚴格遵守啟動順序**：
   務必先啟動 `moq_server`，再啟動 ns-3 模擬器（Bridge），最後再啟動 `moq_client`。若未遵守順序，封包將無法建立映射表或直接被拒絕連線。

2. **確認連接埠未被佔用**：
   在執行 ns-3 模擬前，請確認本機的 `8000` 埠號以及 `4433` 埠號沒有被先前的殘留進程佔用。若發生占用，可使用 `pkill -f moq_server` 或 `pkill -f ns3` 清理背景程序。

3. **Client/Server MTU 對稱性**：
   受限於 MsQuic 底層 PMTUD 在模擬橋接環境中的路徑判斷，Client 與 Server 端務必加上對稱的 `--min_mtu=N` 與 `--max_mtu=N` 具名參數設定，以免交握過程停滯。

4. **UDP 緩衝區最佳化與應用層 Pacing**：
   為了避免在高速影像傳輸時壓垮 ns-3 橋接器並觸發 Linux 核心層級的 UDP 封包遺失，`ClientBridgeApp` 與 `ServerBridgeApp` 會自動將 OS Socket 的接收緩衝區 (`SO_RCVBUF`) 配置為 50MB。此外，建議透過 `moq_client` 的 `--delay=N` 參數開啟應用層 Pacing 機制以平滑發送速率。

5. **ns-3 模擬器統計數據顯示時機**：
   在傳輸過程中，`moq_client` 與 `moq_server` 會即時輸出收發日誌。但 ns-3 模擬器端的橋接封包統計（`tx_packets_` / `rx_packets_` 與 FlowMonitor 監控數據）**必須等待模擬時間到達設定的結束時間，或是手動在終端機按下 `Ctrl + Z` 或 `Ctrl + C` 終止模擬時**才會一次性列印於終端機畫面上並自動關閉。傳輸剛結束時若未見統計數據，屬正常現象。

6. **自動終止模擬時間**：
   模擬腳本預設會在執行 120 秒後自動終止（`Simulator::Stop(Seconds(120.0));`）。若您測試的影片檔過大，或是設定了較長的 Pacing 延遲導致總傳輸時間超過 120 秒，模擬將會被強制中斷。如有需要，您可以直接前往 `ns3/QUIC_sim/two_router_udp.cc` 腳本中修改此時間上限。


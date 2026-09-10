# 環境設定與安裝指引

## 1. 必要前置套件
*   Linux 作業系統 (建議使用 Ubuntu 20.04+)
*   CMake 3.16+
*   OpenSSL 1.1.1+ (用於產生憑證)
*   支援 C++17 的 G++ / GCC 編譯器

## 2. 建置 Microsoft MsQuic
從原始碼下載並建置 MsQuic：
```bash
git clone --recurse-submodules https://github.com/microsoft/msquic.git
cd msquic

# 配置並以偵錯/記錄模式 (Debug/Logging) 編譯 MsQuic
cmake -S . -B build \
  -DCMAKE_BUILD_TYPE=Debug \
  -DQUIC_ENABLE_LOGGING=ON \
  -DQUIC_BUILD_TOOLS=ON

cmake --build build -j$(nproc)
```
確保函式庫路徑符合 CMake 配置。預設情況下，CMake 將指向 `$ENV{HOME}/msquic`，並與 `${MSQUIC_ROOT}/build/bin/Debug/libmsquic.so` 進行連結。

## 3. 產生 TLS 安全憑證
QUIC 協定強制使用加密連線，因此伺服器端需要金鑰與憑證配對。我們可以使用 OpenSSL 產生自簽署的測試憑證。

建議在專案根目錄下建立 `certs/` 資料夾來儲存憑證：
```bash
mkdir -p certs
cd certs

openssl req -x509 -newkey rsa:2048 \
  -keyout server.key \
  -out server.crt \
  -days 365 \
  -nodes \
  -subj "/CN=localhost"
```

## 4. 使用 quicsample 測試 MsQuic
在配置本專案前，您可以使用 MsQuic 內建的測試程式，驗證您的 QUIC 環境與憑證是否正常運作。

啟動測試伺服器端：
```bash
cd msquic/build

./quicsample \
  -server \
  -cert_file:~/certs/server.crt \
  -key_file:~/certs/server.key
```

啟動測試用戶端：
```bash
cd msquic/build

./quicsample \
  -client \
  -target:127.0.0.1 \
  -unsecure
```

---

# 編譯與運行 QUIC 媒體系統

配置並編譯用戶端與伺服器端應用程式：
```bash
# 從專案根目錄出發
mkdir -p build
cd build
cmake ..
make -j$(nproc)
```
這將在 `build/` 目錄下產生兩個可執行檔：
*   `moq_server`：負責接收並寫入檔案的媒體接收端伺服器。
*   `moq_client`：負責解析檔案並進行媒體串流的發送端用戶端。

---

# 使用指南

## 1. 運行伺服器端
先啟動伺服器端監聽連線，以接收串流輸出：
```bash
./moq_server <host> <port> <output.h265/ivf> <cert_file> <key_file> [--min_mtu=N] [--max_mtu=N]
```
*   `--min_mtu=N` / `--max_mtu=N` *(選填)*：強制覆寫 MSQuic 預設的 PMTUD，限制線路上的實體封包大小。
*   **範例**：
    ```bash
    ./moq_server 0.0.0.0 4433 output.h265 ../certs/server.crt ../certs/server.key --min_mtu=1300 --max_mtu=1400
    ```

## 2. 運行用戶端
將輸入的 H.265/IVF 檔案串流傳送至伺服器：
```bash
./moq_client <host> <port> <input.h265/ivf> [--streams=N] [--chunksize=N] [--min_mtu=N] [--max_mtu=N]
```
*   `--streams=N` *(選填)*：要開啟的總通道數量 (預設：`2`，最少 `2` 個通道以區分優先級流量)。
*   `--chunksize=N` *(選填)*：應用層資料切片大小限制，單位為位元組 (預設：`1200`)。
*   `--min_mtu=N` / `--max_mtu=N` *(選填)*：強制覆寫 MSQuic 預設的 PMTUD，限制線路上的實體封包大小。*（備註：受 MsQuic 限制，數值必須符合 `max_mtu >= min_mtu >= 1248`，超出此範圍會導致連線配置失敗）。*
*   **範例**：
    ```bash
    ./moq_client localhost 4433 input.h265 --streams=4 --chunksize=1000 --min_mtu=1300 --max_mtu=1400
    ```

---

# 注意事項與避坑指南

1. **先啟動 Server，再啟動 Client**：
   請務必在 `moq_server` 啟動且開始監聽後再執行 `moq_client`。若 Server 尚未就緒，Client 會因無法建立 QUIC 連線而立刻跳出 `status=113` (Connection Refused) 錯誤退出。*(備註：未來計畫於 Client 端加入連線失敗自動重試機制)*。

2. **Client/Server MTU 必須同步設定**：
   當你在 Client 端設定了 `--max_mtu=N` 時，**Server 端也必須加上對應的 `--max_mtu=N` 設定**。若兩端 MTU 設置不對稱，會導致底層 QUIC 在進行路徑 MTU 探索 (PMTUD) 或傳輸大封包時卡住並發出超時報錯。

3. **MsQuic 的底層 MTU 規範（1248 ~ 1500）**：
   自訂 MTU 數值時，建議確保在 `1500 >= max_mtu >= min_mtu >= 1248` 範圍內。QUIC 最小起始封包長度加表頭為 1248 bytes，低於此數值將直接導致底層 `ConfigurationOpen` 配置失敗。而上限則受限於標準乙太網路的 `1500` bytes，若參數設定超過 `1500`，理論上程序不會崩潰，但 MsQuic 底層防呆機制會在傳輸時自動將其強行修正（Clamp）回 `1500`。

4. **確認憑證路徑正確**：
   啟動 `moq_server` 時需特別留意傳入的 `server.crt` 與 `server.key` 路徑是否正確。若路徑錯誤或檔案不存在，Server 會跳出 `ConfigurationLoadCredential failed` 並立刻結束程序。

5. **清理背景殘留程序**：
   若習慣在背景非同步執行 Server（結尾加 `&`），在下次啟動前請務必執行 `pkill -f moq_server` 關閉既有行程，以免發生連接埠 `4433` 被佔用 (`Address already in use`) 的錯誤。

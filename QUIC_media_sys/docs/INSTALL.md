# Environment Setup & Installation

## 1. Prerequisites
*   Linux OS (Ubuntu 20.04+ recommended)
*   CMake 3.16+
*   OpenSSL 1.1.1+ (for certificate generation)
*   G++ / GCC compiler supporting C++17

## 2. Building Microsoft MsQuic
Cloning and building MsQuic from source:
```bash
git clone --recurse-submodules https://github.com/microsoft/msquic.git
cd msquic

# Configure and compile MsQuic in Debug/Logging mode
cmake -S . -B build \
  -DCMAKE_BUILD_TYPE=Debug \
  -DQUIC_ENABLE_LOGGING=ON \
  -DQUIC_BUILD_TOOLS=ON

cmake --build build -j$(nproc)
```
Ensure the library path matches the CMake configuration. By default, CMake targets `$ENV{HOME}/msquic` and links against `${MSQUIC_ROOT}/build/bin/Debug/libmsquic.so`.

## 3. Generating TLS Certificates
The QUIC protocol mandates encrypted connections, therefore the server needs a key and credential pair. We can use OpenSSL to generate self-signed test credentials.

It is recommended to create a `certs/` folder in the project root directory to store these credentials.
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

## 4. Test msquic with quicsample
Before setting up this project, you can use MsQuic's built-in testing program to ensure that your QUIC environment and credentials are working correctly.

Start the test server:
```bash
cd msquic/build

./quicsample \
  -server \
  -cert_file:~/certs/server.crt \
  -key_file:~/certs/server.key
```

Start the test client:
```bash
cd msquic/build

./quicsample \
  -client \
  -target:127.0.0.1 \
  -unsecure
```



---

# Build and Run QUIC Media System

Configure and compile the client and server applications:
```bash
# From project root directory
mkdir -p build
cd build
cmake ..
make -j$(nproc)
```
This builds two executables in the `build/` directory:
*   `moq_server`: The listening media sink server.
*   `moq_client`: The file parsing and media streaming client.

---

# Usage Guide

## 1. Run the Server
Start the server listener first to receive the streamed output:
```bash
./moq_server <host> <port> <output.h265/ivf> <cert_file> <key_file> [--min_mtu=N] [--max_mtu=N]
```
*   `--min_mtu=N` / `--max_mtu=N` *(Optional)*: Forces MSQuic to override PMTUD and constrain the wire packet sizes.
*   **Example**:
    ```bash
    ./moq_server 0.0.0.0 4433 output.h265 ../certs/server.crt ../certs/server.key --min_mtu=1300 --max_mtu=1400
    ```

## 2. Run the Client
Stream the input H.265/IVF file to the server:
```bash
./moq_client <host> <port> <input.h265/ivf> [--streams=N] [--chunksize=N] [--min_mtu=N] [--max_mtu=N]
```
*   `--streams=N` *(Optional)*: The total number of streams to open (Default: `2`, minimum `2` to split priority flows).
*   `--chunksize=N` *(Optional)*: Payload fragment size limit in bytes (Default: `1200`).
*   `--min_mtu=N` / `--max_mtu=N` *(Optional)*: Forces MSQuic to override PMTUD and constrain the wire packet sizes. *(Note: Must satisfy `max_mtu >= min_mtu >= 1248` due to MsQuic limits; invalid values will cause initialization to fail).*
*   **Example**:
    ```bash
    ./moq_client localhost 4433 input.h265 --streams=4 --chunksize=1000 --min_mtu=1300 --max_mtu=1400
    ```

---

# Important Notes & Best Practices

1. **Server First, Client Second**:
   Always start `moq_server` and let it initialize before running `moq_client`. If the server is not listening, the client will immediately fail with a transport connection error (e.g., `status=113`, Connection Refused). *(Note: Automatic reconnection/retry logic may be introduced in future client versions)*.

2. **Symmetric MTU Configuration**:
   If you configure `--max_mtu=N` on `moq_client`, **you must also configure `--max_mtu=N` on `moq_server`**. Otherwise, asymmetric MTU discovery settings will cause the QUIC connection handshake or data transfer to stall and eventually error out.

3. **MsQuic MTU Limits (1248 ~ 1500)**:
   Ensure any configured MTU values adhere to `1500 >= max_mtu >= min_mtu >= 1248`. Values below `1248` violate QUIC base specifications and will cause `ConfigurationOpen` to fail. If you configure values exceeding the standard Ethernet MTU ceiling of `1500`, MsQuic will not crash but will automatically clamp the MTU back down to `1500` during transmission as a built-in safeguard.

4. **Valid Certificate Paths**:
   Make sure the server is provided with correct paths to `server.crt` and `server.key`. If either file is missing, the server will fail during initial credential loading (`ConfigurationLoadCredential failed`).

5. **Background Process Cleanup**:
   When running `moq_server` in the background (`&`), ensure you terminate it (`pkill -f moq_server`) before starting a new test run to prevent `Address already in use` (port `4433` collision) errors.
import sys
import os
import time
import threading
import datetime

# 將 build 目錄加入系統路徑
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), 'lib')))

from quic_connection import QuicConnection

def send_data(stream, name, count):
    print(f"[{name}] Starting to send {count} frames on stream {stream.stream_id}...")
    start_time = time.time()
    for i in range(count):
        # 放大資料量，每包 100KB，確保能塞滿發送緩衝區以觸發優先權排程
        payload = f"{name}_FRAME_{i}_".encode('utf-8')
        payload += b'*' * (100000 - len(payload))
        
        # 突發狀況：假設傳到一半時，我們把兩邊的優先權完全對調！
        if i == (count // 2):
            if name == "LOW_PRIORITY":
                print(f"\n🚨 [DYNAMIC PRIORITY] {name} (Stream {stream.stream_id}) is upgraded to HIGHEST (0)! 🚨\n")
                stream._client.set_stream_priority(stream.stream_id, 0)
            elif name == "HIGH_PRIORITY":
                print(f"\n🚨 [DYNAMIC PRIORITY] {name} (Stream {stream.stream_id}) is downgraded to LOWEST (65535)! 🚨\n")
                stream._client.set_stream_priority(stream.stream_id, 65535)

        # 為了避免 log 洗畫面太快，只印出部分進度
        if i % 50 == 0:
            now_str = datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3]
            print(f"[Queueing] Time: {now_str} | Stream ID: {stream.stream_id} | Progress: {i}/{count} frames")
        
        # send_data 是非同步的，它只會把指標交給 MsQuic 就立刻返回
        stream.send_data(frame_id=i, data=payload)
        
        # 拿掉原本的 33ms sleep，改為極短的 sleep (1ms)，瘋狂塞資料給 MsQuic
        time.sleep(0.001)
        
    elapsed = time.time() - start_time
    print(f"[{name}] Finished queuing all {count} frames to MsQuic in {elapsed:.3f} seconds!")

def main():
    print("=== QUIC Stream Priority Test ===")
    
    # 這裡我們使用 ROUND_ROBIN (預設 scheduling_scheme=1)
    host = "127.0.0.1" # match test_stream_add_drop
    port = 4433
    conn = QuicConnection(host=host, port=port, scheduling_scheme=1)

    # 1. 開啟低優先權通道
    low_stream = conn.open_stream()
    conn._client.set_stream_priority(low_stream.stream_id, 65535) # 65535 = 最低優先權

    # 2. 開啟高優先權通道
    high_stream = conn.open_stream()
    conn._client.set_stream_priority(high_stream.stream_id, 0) # 0 = 最高優先權

    print(f"Low Priority Stream ID: {low_stream.stream_id}")
    print(f"High Priority Stream ID: {high_stream.stream_id}")

    # 要傳送的數量：每個通道各傳 500 張圖 (500 * 30KB)
    frames_to_send = 500
    
    # 建立兩個執行緒，模擬「同時間」有兩個不同來源的影片在傳送
    t1 = threading.Thread(target=send_data, args=(low_stream, "LOW_PRIORITY", frames_to_send))
    t2 = threading.Thread(target=send_data, args=(high_stream, "HIGH_PRIORITY", frames_to_send))

    print("\n[Race Started] Aggressively queuing data to both streams...\n")
    t1.start()
    t2.start()

    t1.join()
    t2.join()

    print("\n[Queuing Done] All frames given to MsQuic.")
    print("Waiting 10 seconds for MsQuic to actually finish transmitting everything over the network...")
    time.sleep(10)
    
    low_stream.close()
    high_stream.close()
    time.sleep(1)
    
    print("\n-> 呼叫 disconnect()")
    conn.disconnect()

if __name__ == "__main__":
    main()

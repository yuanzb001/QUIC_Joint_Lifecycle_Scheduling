import sys
import os
import time

# 將 build 目錄加入系統路徑
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), 'lib')))

from quic_connection import QuicConnection

def main():
    print("=== QUIC OOP Connection & Stream Test ===")
    
    # 建立連線物件 (內部已經實作了 QuicMediaClient 和 connect)
    # 這裡我們使用 ROUND_ROBIN (預設 scheduling_scheme=1)
    conn = QuicConnection(host="10.11.29.34", port=4433, scheduling_scheme=1)

    print("\n--- [Step 1] 測試手動開 Stream ---")
    stream1 = conn.open_stream()
    print(f"手動開啟 Stream，ID: {stream1.stream_id}")
    
    # 用包裝好的物件送資料
    stream1.send_data(frame_id=0, data=b"Hello QUIC OOP!")
    print(f"Stream {stream1.stream_id} 狀態: active={stream1.is_active}, bytes_sent={stream1.bytes_sent}")
    
    # 優雅關閉
    stream1.close()
    print(f"Stream {stream1.stream_id} 關閉後狀態: active={stream1.is_active}")

    print("\n--- [Step 2] 測試定時自動產生 Stream ---")
    # 每 1 秒自動開啟一條新 Stream，每條 Stream 自動送 50 張影像
    conn.start_stream_generator(interval=1.0, frames_per_stream=100, delay=0.033, payload_size=1024*1024)
    
    # 讓產生器跑 3.5 秒 (應該會產生約 3-4 條 Stream)
    time.sleep(3.5)
    
    print("\n--- [Step 3] 測試傳輸中斷 (Drop Stream) ---")
    # 選擇目前的一條 stream，在它送資料送到一半時把它 drop 掉
    active_streams = [s for s in conn.streams if s.is_active]
    if active_streams:
        target_stream = active_streams[-1]
        print(f"\n-> 準備中斷 Stream {target_stream.stream_id}...")
        target_stream.drop()
        print(f"-> Stream {target_stream.stream_id} 已發送 Drop 訊號！等待背景 Thread 處理錯誤。")
        time.sleep(1.0) # 等待 1 秒讓背景 thread 觸發 enqueue 失敗
        
    print("\n--- [Step 4] 停止自動產生，準備強制斷線 ---")
    conn.stop_stream_generator()
    
    # 查看目前擁有的 Streams
    print("\n目前管理的 Streams:")
    for s in conn.streams:
        print(f" - Stream {s.stream_id} | 狀態: active={s.is_active} | 送出張數: {s.frames_sent}")
        
    print("\n-> 呼叫 disconnect(force=True)")
    # disconnect 會自動等待 worker threads 清理完畢
    conn.disconnect(force=True)
    
    print("\n=== Test Finished ===")

if __name__ == "__main__":
    main()

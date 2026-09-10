-- ============================================================
--  moq_dissector.lua  (version 3.0 — Post-dissector)
--
--  【設計原則】
--  使用 register_postdissector() 而不是 DissectorTable。
--  Wireshark 會先完整顯示所有內建的 QUIC / TLS 欄位，
--  本 plugin 只在最後附加一個 "MoQ PacketHeader" subtree，
--  不會破壞任何原有顯示。
--
--  PacketHeader (wire.hpp, 16 bytes, little-endian):
--    [0-3]  uint32_t  nalu_id
--    [4-7]  uint32_t  payload_len
--    [8-9]  uint16_t  flags
--    [A-B]  uint16_t  reserved
--    [C-D]  uint16_t  fragment_idx
--    [E-F]  uint16_t  fragment_count
--    [10..] payload   H.265 bytes
-- ============================================================

if _G["moq_proto_loaded"] then return end
_G["moq_proto_loaded"] = true

local MOQ_HDR_LEN = 16

-- ---- Proto & Fields ----------------------------------------

local ok, moq_proto = pcall(Proto, "moq", "MoQ PacketHeader (H.265 over QUIC)")
if not ok then return end

local f_nalu_id        = ProtoField.uint32("moq.nalu_id",        "NALU ID",           base.DEC)
local f_payload_len    = ProtoField.uint32("moq.payload_len",    "Payload Length",    base.DEC)
local f_flags          = ProtoField.uint16("moq.flags",          "Flags",             base.HEX)
local f_reserved       = ProtoField.uint16("moq.reserved",       "Reserved",          base.HEX)
local f_fragment_idx   = ProtoField.uint16("moq.fragment_idx",   "Fragment Index",    base.DEC)
local f_fragment_count = ProtoField.uint16("moq.fragment_count", "Fragment Count",    base.DEC)
local f_nal_type       = ProtoField.uint8 ("moq.nal_type",       "H.265 NAL Type",    base.DEC)

moq_proto.fields = {
    f_nalu_id, f_payload_len, f_flags, f_reserved,
    f_fragment_idx, f_fragment_count, f_nal_type,
}

-- ---- Field Extractors (讀取已解析的 QUIC 欄位) --------------
--  quic.stream_data: 解密後的 QUIC STREAM frame payload
--  quic.stream.stream_id: 對應的 stream ID

local quic_stream_data_f = Field.new("quic.stream_data")
local quic_stream_id_f   = Field.new("quic.stream.stream_id")

-- ---- H.265 NAL type 名稱表 ----------------------------------

local NAL_NAMES = {
    [0]  = "TRAIL_N",   [1]  = "TRAIL_R",   [2]  = "TSA_N",
    [3]  = "TSA_R",     [4]  = "STSA_N",    [5]  = "STSA_R",
    [16] = "BLA_W_LP",  [19] = "IDR_W_RADL",[20] = "IDR_N_LP",
    [21] = "CRA_NUT",   [32] = "VPS",        [33] = "SPS",
    [34] = "PPS",        [35] = "AUD",
}

-- ---- Post-dissector 主函數 ----------------------------------

function moq_proto.dissector(tvbuf, pktinfo, tree)

    -- 抓出所有 QUIC stream data 欄位值 (一個 UDP 封包裡可能有多個 STREAM frame)
    local stream_data_vals = { quic_stream_data_f() }
    local stream_id_vals   = { quic_stream_id_f() }

    -- 沒有 QUIC stream data → 不是我們想解析的封包，直接返回
    if #stream_data_vals == 0 then return end

    for i, sdv in ipairs(stream_data_vals) do
        -- sdv.range 是 TvbRange，代表這個 STREAM frame 的 payload bytes
        local rng = sdv.range
        if rng == nil then goto continue end

        local data_len = rng:len()
        if data_len < MOQ_HDR_LEN then goto continue end

        -- 取對應的 stream_id（數量可能跟 stream_data 不完全一致，取 i 或最後一個）
        local sid = -1
        if stream_id_vals[i] then
            sid = stream_id_vals[i].value
        elseif stream_id_vals[1] then
            sid = stream_id_vals[1].value
        end

        -- 讀取 PacketHeader 欄位 (little-endian)
        local nalu_id       = rng(0, 4):le_uint()
        local payload_len   = rng(4, 4):le_uint()
        local flags         = rng(8, 2):le_uint()
        local reserved      = rng(10, 2):le_uint()
        local fragment_idx  = rng(12, 2):le_uint()
        local fragment_count= rng(14, 2):le_uint()

        -- 嚴格合理性檢查：過濾掉被當成 Header 的「純影像碎片」
        -- 你的設定 chunk_size 是 1200，且 flags/reserved 必定為 0
        if flags ~= 0 then goto continue end
        if reserved ~= 0 then goto continue end
        if payload_len > 8192 then goto continue end -- 防呆上限
        if fragment_count == 0 then goto continue end
        if fragment_idx >= fragment_count then goto continue end

        -- ---- 建立附加的 subtree，掛在封包詳情最下方 ----------
        local frag_str
        if fragment_count <= 1 then
            frag_str = string.format("NALU #%d  len=%d", nalu_id, payload_len)
        else
            frag_str = string.format("NALU #%d  frag %d/%d  len=%d",
                nalu_id, fragment_idx + 1, fragment_count, payload_len)
        end

        local sid_str = (tostring(sid) ~= "-1") and string.format("  [Stream ID %s]", tostring(sid)) or ""
        local title   = string.format("MoQ PacketHeader  —  %s%s", frag_str, sid_str)

        local subtree = tree:add(moq_proto, rng(0, MOQ_HDR_LEN), title)

        subtree:add_le(f_nalu_id,        rng(0,  4))
        subtree:add_le(f_payload_len,    rng(4,  4))
        subtree:add_le(f_flags,          rng(8,  2))
        subtree:add_le(f_reserved,       rng(10, 2))
        subtree:add_le(f_fragment_idx,   rng(12, 2))
        subtree:add_le(f_fragment_count, rng(14, 2))

        -- H.265 NAL type（如果有 payload）
        if payload_len > 0 and data_len > MOQ_HDR_LEN then
            local b0 = rng(MOQ_HDR_LEN, 1):uint()
            local nal_type = bit.band(bit.rshift(b0, 1), 0x3F)
            local nal_name = NAL_NAMES[nal_type]
                          or string.format("NAL_TYPE_%d", nal_type)
            subtree:add(f_nal_type, rng(MOQ_HDR_LEN, 1))
                :set_text(string.format("H.265 NAL Type: %s (%d)", nal_name, nal_type))
        end

        -- fragment 提示
        if fragment_count > 1 then
            if fragment_idx == 0 then
                subtree:add_expert_info(PI_SEQUENCE, PI_CHAT,
                    string.format("First fragment of NALU #%d (%d total fragments)",
                        nalu_id, fragment_count))
            elseif fragment_idx == fragment_count - 1 then
                subtree:add_expert_info(PI_SEQUENCE, PI_CHAT,
                    string.format("Last fragment of NALU #%d", nalu_id))
            end
        end

        ::continue::
    end
end

-- ---- 註冊為 Post-dissector ----------------------------------
--  這樣 Wireshark 會先跑完所有內建 dissector（包含 QUIC 解密），
--  再呼叫我們的函數附加 MoQ subtree，原有顯示完全不受影響。
register_postdissector(moq_proto)

print("[moq_dissector] v3.0 loaded as post-dissector — original QUIC display preserved")

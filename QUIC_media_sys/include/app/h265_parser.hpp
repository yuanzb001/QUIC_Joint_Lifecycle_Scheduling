#pragma once
#include <vector>
#include <cstdint>
#include <string>

namespace moq::app {

/**
 * @brief Represents a single H.265 Network Abstraction Layer Unit (NALU).
 * 
 * Contains a pointer into the raw file memory, the size of the NALU,
 * and the size of the start code prefix preceding it.
 */
struct Nalu {
    const uint8_t* data;   ///< Non-owning pointer to the beginning of the Nalu (including start code prefix).
    size_t size;           ///< Total size of the NALU including start code prefix.
    size_t prefix_len;     ///< Length of the start code prefix (3 bytes for 00 00 01, or 4 bytes for 00 00 00 01).

    /**
     * @brief Extracts and returns the H.265 NALU type.
     * 
     * H.265 NALU header is 2 bytes; the type is located in the first byte,
     * bits 1 to 6 (0-indexed, where bit 0 is Forbidden Zero Bit).
     * 
     * @return 6-bit NALU type value, or 0 if data size is smaller than the prefix.
     */
    uint8_t get_nalu_type() const {
        if (size <= prefix_len) return 0;
        return (data[prefix_len] >> 1) & 0x3F;
    }

    /**
     * @brief Determines if this NALU is critical for video decoding metadata or synchronization.
     * 
     * Important NALUs include VPS (Video Parameter Set), SPS (Sequence Parameter Set),
     * PPS (Picture Parameter Set), and IRAP/I-frames (types 16 to 21).
     * 
     * @return true if the NALU is important, false otherwise.
     */
    bool is_important() const;
};

/**
 * @brief Parser to read H.265 raw annex-B format stream files.
 * 
 * Loads the input video file entirely into memory and scans for
 * annex-B start codes to segment it into an array of separate NALUs.
 */
class H265FileParser {
public:
    /**
     * @brief Constructor for H265FileParser.
     * @param filepath The path to the input H.265 annex-B stream file.
     */
    explicit H265FileParser(const std::string& filepath);
    
    /**
     * @brief Opens, reads the entire file into buffer memory, and parses all NALUs.
     * @return true on success, false if file open/read fails or no NALUs are found.
     */
    bool open();
    
    /**
     * @brief Retrieve the list of parsed NALUs.
     * @return Const reference to vector containing parsed Nalu structures.
     */
    const std::vector<Nalu>& get_nalus() const { return nalus_; }

private:
    std::string filepath_;            ///< Path of the input stream file.
    std::vector<uint8_t> buffer_;     ///< Buffer hosting the loaded file contents.
    std::vector<Nalu> nalus_;         ///< List of parsed NALU descriptors.

    /**
     * @brief Locate the next Annex-B start code starting from a given offset.
     * 
     * Annex-B start codes are either 3 bytes (0x000001) or 4 bytes (0x00000001).
     * 
     * @param start_pos The byte index offset to start searching from.
     * @return A pair containing:
     *         - The absolute offset of the start code in the buffer.
     *         - The length of the start code prefix (3 or 4 bytes).
     *         If not found, returns {buffer.size(), 0}.
     */
    std::pair<size_t, size_t> find_start_code(size_t start_pos) const;
};

} // namespace moq::app


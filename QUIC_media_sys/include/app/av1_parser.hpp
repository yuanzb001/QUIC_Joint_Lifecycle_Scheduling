#pragma once
#include <vector>
#include <cstdint>
#include <string>

namespace moq::app {

/**
 * @brief Represents a single AV1 frame extracted from an IVF file.
 * 
 * Contains a pointer into the raw file memory, the size of the frame data,
 * and the presentation timestamp (PTS).
 */
struct Av1Frame {
    const uint8_t* data;   ///< Non-owning pointer to the beginning of the frame data.
    size_t size;           ///< Total size of the frame data.
    uint64_t pts;          ///< Presentation timestamp from the IVF frame header.

    /**
     * @brief Determines if this frame is critical (e.g. Sequence Header).
     * 
     * In AV1, an OBU has a header where bits 6-3 indicate the type.
     * OBU type 1 is Sequence Header.
     * 
     * @return true if the frame is deemed important, false otherwise.
     */
    // is_important: Determines if the current AV1 frame contains critical decoding data.
    // 1. Skip the encapsulated IVF global header (32 bytes) and frame header (12 bytes) if present.
    // 2. Read the OBU (Open Bitstream Unit) type from the core payload.
    // 3. If it's a Sequence Header (obu_type == 1), it contains global config and is marked as highly important.
    // 4. Return true so the client can route this crucial frame via the highest priority QUIC stream.
    bool is_important() const;
};

/**
 * @brief Parser to read AV1 IVF format stream files.
 * 
 * Loads the input video file entirely into memory and parses
 * the IVF headers to segment it into an array of separate Av1Frames.
 */
class Av1IvfParser {
public:
    /**
     * @brief Constructor for Av1IvfParser.
     * @param filepath The path to the input IVF stream file.
     */
    explicit Av1IvfParser(const std::string& filepath);
    
    /**
     * @brief Opens, reads the entire file into buffer memory, and parses all IVF frames.
     * @return true on success, false if file open/read fails or invalid IVF header.
     */
    bool open();
    
    /**
     * @brief Retrieve the list of parsed AV1 frames.
     * @return Const reference to vector containing parsed Av1Frame structures.
     */
    const std::vector<Av1Frame>& get_frames() const { return frames_; }

private:
    std::string filepath_;              ///< Path of the input stream file.
    std::vector<uint8_t> buffer_;       ///< Buffer hosting the loaded file contents.
    std::vector<Av1Frame> frames_;      ///< List of parsed AV1 frame descriptors.
};

} // namespace moq::app

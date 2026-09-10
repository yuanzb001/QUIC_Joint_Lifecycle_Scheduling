#include "app/h265_parser.hpp"
#include <fstream>
#include <iostream>

namespace moq::app {

bool Nalu::is_important() const {
    uint8_t type = get_nalu_type();
    // Types 32=VPS, 33=SPS, 34=PPS, 16~21=I-frames (IRAP pictures)
    return (type >= 16 && type <= 21) || (type >= 32 && type <= 34);
}

H265FileParser::H265FileParser(const std::string& filepath) : filepath_(filepath) {}

bool H265FileParser::open() {
    // Open the stream file at the end to quickly determine its size
    std::ifstream file(filepath_, std::ios::binary | std::ios::ate);
    if (!file.is_open()) {
        std::cerr << "Can't open file: " << filepath_ << "\n";
        return false;
    }
    
    std::streamsize size = file.tellg();
    file.seekg(0, std::ios::beg);
    
    if (size <= 0) return false;
    
    // Read the entire file contents directly into memory for efficient annex-B scanning
    // TODO: support stream-based buffered reads for extremely large files
    buffer_.resize(size);
    if (!file.read(reinterpret_cast<char*>(buffer_.data()), size)) {
        std::cerr << "File read fail\n";
        return false;
    }

    size_t offset = 0;
    std::pair<size_t, size_t> current_sc = find_start_code(offset);
    
    // Loop and scan for all NALUs (segment between one Start Code and the next)
    while (current_sc.first < buffer_.size()) {
        size_t start = current_sc.first;
        size_t prefix_len = current_sc.second;
        
        // Find the next Start Code from the current Start Code position
        std::pair<size_t, size_t> next_sc = find_start_code(start + prefix_len);
        
        // The size of this NALU is the difference between the two Start Code positions
        size_t nalu_size = next_sc.first - start;
        nalus_.push_back({buffer_.data() + start, nalu_size, prefix_len});
        
        current_sc = next_sc; // Move to the next NALU
    }
    
    return !nalus_.empty();
}

std::pair<size_t, size_t> H265FileParser::find_start_code(size_t start_pos) const {
    // Scan buffer seeking for standard annex-B start patterns (0x000001 or 0x00000001)
    for (size_t i = start_pos; i + 2 < buffer_.size(); ++i) {
        if (buffer_[i] == 0x00 && buffer_[i+1] == 0x00) {
            if (buffer_[i+2] == 0x01) {
                return {i, 3}; // Found 00 00 01
            }
            if (i + 3 < buffer_.size() && buffer_[i+2] == 0x00 && buffer_[i+3] == 0x01) {
                return {i, 4}; // Found 00 00 00 01
            }
        }
    }
    // Return end of stream if start code not found
    return {buffer_.size(), 0};
}

} // namespace moq::app


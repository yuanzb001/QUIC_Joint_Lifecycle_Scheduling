#include "app/av1_parser.hpp"
#include <fstream>
#include <iostream>
#include <cstring>

namespace moq::app {

bool Av1Frame::is_important() const {
    if (size == 0) return false;
    
    size_t offset = 0;
    if (size >= 32 && data[0] == 'D' && data[1] == 'K' && data[2] == 'I' && data[3] == 'F') {
        offset += 32;
    }
    
    if (offset + 12 <= size) {
        offset += 12;
    }
    
    while (offset < size) {
        uint8_t header = data[offset];
        uint8_t obu_type = (header >> 3) & 0x0F;
        uint8_t has_size_field = (header >> 1) & 0x01;
        uint8_t extension_flag = (header >> 2) & 0x01;
        
        if (obu_type == 1) {
            return true;
        }
        
        if (obu_type == 2) {
            // Skip temporal delimiter and check next OBU
        } else {
            break;
        }
        
        offset++;
        if (extension_flag) offset++;
        if (has_size_field) {
            break;
        }
    }
    
    return false;
}

Av1IvfParser::Av1IvfParser(const std::string& filepath) : filepath_(filepath) {}

// open: Parses the .ivf file and encapsulates global/frame headers into the frames.
// To achieve zero-overhead reassembly at the server side, we "smuggle" the IVF headers inside the payload.
// 1. Read the entire file into memory and validate the 32-byte global header and "DKIF" signature.
// 2. Loop through the buffer to locate the 12-byte frame headers.
// 3. For the first frame, expand the chunk size to include the 32-byte global header + 12-byte frame header.
// 4. For subsequent frames, expand the chunk size to include their 12-byte frame headers.
// 5. Store these expanded chunks into frames_, allowing the server to write them directly to disk.
bool Av1IvfParser::open() {
    std::ifstream file(filepath_, std::ios::binary | std::ios::ate);
    if (!file.is_open()) {
        std::cerr << "Can't open file: " << filepath_ << "\n";
        return false;
    }
    
    std::streamsize size = file.tellg();
    file.seekg(0, std::ios::beg);
    
    if (size <= 32) {
        std::cerr << "File too small to be a valid IVF file.\n";
        return false;
    }
    
    buffer_.resize(size);
    if (!file.read(reinterpret_cast<char*>(buffer_.data()), size)) {
        std::cerr << "File read fail\n";
        return false;
    }

    if (buffer_[0] != 'D' || buffer_[1] != 'K' || buffer_[2] != 'I' || buffer_[3] != 'F') {
        std::cerr << "Invalid IVF signature.\n";
        return false;
    }

    size_t offset = 0;

    // Read frames
    while (offset + 32 + 12 <= buffer_.size() || (offset > 0 && offset + 12 <= buffer_.size())) {
        size_t current_header_offset = offset;
        bool is_first_frame = (offset == 0);
        
        if (is_first_frame) {
            current_header_offset += 32; // Skip 32-byte global header to read frame size
        }

        uint32_t frame_size = static_cast<uint32_t>(buffer_[current_header_offset]) |
                              (static_cast<uint32_t>(buffer_[current_header_offset + 1]) << 8) |
                              (static_cast<uint32_t>(buffer_[current_header_offset + 2]) << 16) |
                              (static_cast<uint32_t>(buffer_[current_header_offset + 3]) << 24);
        
        uint64_t pts = static_cast<uint64_t>(buffer_[current_header_offset + 4]) |
                       (static_cast<uint64_t>(buffer_[current_header_offset + 5]) << 8) |
                       (static_cast<uint64_t>(buffer_[current_header_offset + 6]) << 16) |
                       (static_cast<uint64_t>(buffer_[current_header_offset + 7]) << 24) |
                       (static_cast<uint64_t>(buffer_[current_header_offset + 8]) << 32) |
                       (static_cast<uint64_t>(buffer_[current_header_offset + 9]) << 40) |
                       (static_cast<uint64_t>(buffer_[current_header_offset + 10]) << 48) |
                       (static_cast<uint64_t>(buffer_[current_header_offset + 11]) << 56);

        size_t chunk_size = (is_first_frame ? 32 : 0) + 12 + frame_size;

        if (offset + chunk_size > buffer_.size()) {
            std::cerr << "Warning: Incomplete frame at the end of the IVF file.\n";
            break;
        }

        frames_.push_back({buffer_.data() + offset, chunk_size, pts});
        offset += chunk_size;
    }

    return !frames_.empty();
}

} // namespace moq::app

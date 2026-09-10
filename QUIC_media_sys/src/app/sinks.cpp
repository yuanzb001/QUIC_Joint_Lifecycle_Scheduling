#include "app/sinks.hpp"
#include <stdexcept>

namespace moq::app {

FileSink::FileSink(const std::string& path)
    : ofs_(path, std::ios::binary) {
    // Open target file in binary mode. Throw runtime_error if standard output streaming setup fails.
    if (!ofs_) throw std::runtime_error("Failed to open sink file: " + path);
}

void FileSink::write(const uint8_t* data, size_t len) {
    // Cast and write target data slice directly to binary file descriptor stream.
    ofs_.write(reinterpret_cast<const char*>(data), static_cast<std::streamsize>(len));
    // Force immediate write to physical disk to prevent data loss.
    ofs_.flush();
}

void FileSink::flush() { ofs_.flush(); }

} // namespace moq::app
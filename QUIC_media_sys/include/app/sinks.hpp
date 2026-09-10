#pragma once
#include <cstddef>
#include <cstdint>
#include <fstream>
#include <string>

namespace moq::app {

/**
 * @brief Output stream helper to write binary data into local files.
 */
class FileSink {
public:
    /**
     * @brief Constructor for FileSink.
     * @param path Target filepath to write the binary stream to.
     * @throws std::runtime_error if file cannot be opened.
     */
    explicit FileSink(const std::string& path);

    /**
     * @brief Writes bytes to the local file output stream.
     * @param data Pointer to the buffer holding the data to write.
     * @param len Number of bytes to write.
     */
    void write(const uint8_t* data, size_t len);

    /**
     * @brief Flushes any buffered bytes to disk.
     */
    void flush();

private:
    std::ofstream ofs_;                ///< Underlying file stream descriptor.
};

} // namespace moq::app
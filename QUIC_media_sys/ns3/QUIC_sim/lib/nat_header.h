#pragma once
#include <cstdint>

#pragma pack(push, 1)
struct NatHeader {
    uint32_t real_ip;
    uint16_t real_port;
};
#pragma pack(pop)

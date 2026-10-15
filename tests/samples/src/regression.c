// Ordinary code that must produce zero rewrites.
#include <stdint.h>
#ifdef _WIN32
#define EXPORT __declspec(dllexport) __attribute__((noinline, used))
#else
#define EXPORT __attribute__((noinline, used, visibility("default")))
#endif

EXPORT uint32_t reg_crc32_step(uint32_t crc, uint32_t byte) {
    crc ^= byte;
    for (int k = 0; k < 8; k++) crc = (crc >> 1) ^ (0xEDB88320u & (0u - (crc & 1u)));
    return crc;
}
EXPORT uint32_t reg_fnv1a(uint32_t h, uint32_t byte) { return (h ^ byte) * 16777619u; }
EXPORT uint32_t reg_fmix32(uint32_t h) {
    h ^= h >> 16; h *= 0x85ebca6bu; h ^= h >> 13; h *= 0xc2b2ae35u; h ^= h >> 16;
    return h;
}
EXPORT uint32_t reg_popcount(uint32_t x) {
    x = x - ((x >> 1) & 0x55555555u);
    x = (x & 0x33333333u) + ((x >> 2) & 0x33333333u);
    x = (x + (x >> 4)) & 0x0F0F0F0Fu;
    return (x * 0x01010101u) >> 24;
}
EXPORT uint32_t reg_rotl(uint32_t x, uint32_t r) { return (x << (r & 31)) | (x >> ((32 - r) & 31)); }
EXPORT uint32_t reg_clear_lowest(uint32_t x) { return x & (x - 1u); }
EXPORT uint32_t reg_average(uint32_t x, uint32_t y) { return (x & y) + ((x ^ y) >> 1); }
EXPORT uint32_t reg_abs_diff(int32_t x, int32_t y) { int32_t d = x - y; int32_t m = d >> 31; return (uint32_t)((d ^ m) - m); }

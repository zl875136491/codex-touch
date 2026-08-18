// AXP2101 PEK and battery access for Waveshare ESP32-S3-Touch-AMOLED-1.75C.
#pragma once

#include <lgfx/v1/platforms/common.hpp>

namespace axp {

static constexpr int kPort = I2C_NUM_1;
static constexpr uint8_t kAddr = 0x34;
static constexpr uint32_t kFreq = 400000;
static constexpr uint8_t kRegStatus = 0x00;
static constexpr uint8_t kRegCharge = 0x01;
static constexpr uint8_t kRegChipId = 0x03;
static constexpr uint8_t kRegAdc = 0x30;
static constexpr uint8_t kRegSoc = 0xA4;
static constexpr uint8_t kRegPek = 0x49;

inline uint8_t readReg(uint8_t reg) {
  auto value = lgfx::i2c::readRegister8(kPort, kAddr, reg, kFreq);
  return value.has_value() ? value.value() : 0xFF;
}

inline bool writeReg(uint8_t reg, uint8_t value) {
  return lgfx::i2c::writeRegister8(kPort, kAddr, reg, value, 0, kFreq).has_value();
}

inline bool init() {
  lgfx::i2c::init(kPort, GPIO_NUM_15, GPIO_NUM_14);
  if (readReg(kRegStatus) == 0xFF || readReg(kRegChipId) != 0x4A) return false;
  writeReg(kRegAdc, 0b111111);
  return true;
}

inline int8_t batteryPercent() {
  const uint8_t value = readReg(kRegSoc);
  return value <= 100 ? static_cast<int8_t>(value) : -1;
}

// Returns 0 for no event, 1 for long press, 2 for short press. Reading clears it.
inline uint8_t pekPress() {
  const uint8_t value = readReg(kRegPek) & 0x0C;
  if (value) writeReg(kRegPek, value);
  return value >> 2;
}

}  // namespace axp

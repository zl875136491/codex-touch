// Codex Touch board client for Waveshare ESP32-S3-Touch-AMOLED-1.75C.
// Codex authentication stays on the local Python relay server.
#include <Arduino.h>
#include <ArduinoJson.h>
#include <HTTPClient.h>
#include <M5Unified.h>
#include <WiFi.h>
#include <math.h>
#include <time.h>

#include "Axp2101Power.h"

#if __has_include("TouchConfig.h")
#include "TouchConfig.h"
#else
#define TOUCH_WIFI_SSID ""
#define TOUCH_WIFI_PASSWORD ""
#define TOUCH_SERVER_HOST "127.0.0.1"
#define TOUCH_SERVER_PORT 8765
#define TOUCH_SERVER_TOKEN ""
#endif

namespace {
constexpr int kWidth = 466;
constexpr int kAgentCount = 6;
constexpr int kAgentSlots = 7;
constexpr int kSwipeThreshold = 90;
constexpr int kCenterX = 233;
constexpr int kCenterY = 233;
constexpr int kUiRadius = 225;
constexpr int kSectorOuter = 218;
constexpr int kSectorInner = 76;
constexpr uint32_t kPollMs = 1500;
constexpr uint32_t kWifiRetryMs = 10000;
constexpr uint32_t kTelemetryMs = 1000;
constexpr uint32_t kPowerTouchGuardMs = 450;
constexpr uint32_t kTransitionTouchGuardMs = 180;
constexpr float kMinTextSize = 1.35f;
constexpr uint32_t kBackground = 0x070B10;
constexpr uint32_t kPanel = 0x111820;
constexpr size_t kJsonCapacity = 16384;

enum class View : uint8_t { Home, Tasks, Approval, Agents, AgentDetail };
enum class SlideFrom : uint8_t { Left, Right, Top, Bottom };

M5Canvas mainCanvas(&M5.Display);
M5Canvas transitionCanvas(&M5.Display);
M5Canvas statusCanvas(&M5.Display);
M5Canvas* activeCanvas = &mainCanvas;
#define canvas (*activeCanvas)

String stateJson;
View currentView = View::Home;
String selectedAgentId;
uint32_t lastPoll = 0;
uint32_t lastWifiAttempt = 0;
uint32_t lastPowerPoll = 0;
uint32_t lastTelemetryPoll = 0;
bool axpReady = false;
int8_t batteryLevel = -1;
int wifiLevel = 0;
bool serverReachable = false;
uint32_t serverLatencyMs = 0;
bool touching = false;
int touchStartX = 0;
int touchStartY = 0;
uint32_t ignoreTouchUntil = 0;
bool requireFreshTouch = false;

void discardNavigationTouch(uint32_t durationMs);

String apiUrl(const char* path) {
  return String("http://") + TOUCH_SERVER_HOST + ":" + String(TOUCH_SERVER_PORT) + path;
}

uint16_t rgb(uint32_t value) {
  return canvas.color565((value >> 16) & 255, (value >> 8) & 255, value & 255);
}

void centered(const String& value, int x, int y, uint16_t color = TFT_WHITE, float size = 1.0f) {
  canvas.setTextDatum(middle_center);
  canvas.setTextColor(color);
  canvas.setTextSize(max(kMinTextSize, size));
  canvas.drawString(value, x, y);
}

void text(const String& value, int x, int y, uint16_t color = TFT_WHITE, float size = 1.0f) {
  canvas.setTextDatum(middle_left);
  canvas.setTextColor(color);
  canvas.setTextSize(max(kMinTextSize, size));
  canvas.drawString(value, x, y);
}

void centeredWrapped(const String& value, int x, int y, int maxChars, uint16_t color,
                     float size, int lineGap = 24, int maxLines = 3) {
  String rest = value;
  rest.trim();
  int line = 0;
  while (rest.length() && line < maxLines) {
    if (rest.length() <= static_cast<unsigned>(maxChars)) {
      centered(rest, x, y + line * lineGap, color, size);
      return;
    }
    int cut = min(maxChars, static_cast<int>(rest.length()));
    while (cut > 0 && rest.charAt(cut) != ' ') --cut;
    if (cut == 0) cut = maxChars;
    String part = rest.substring(0, cut);
    part.trim();
    if (line == maxLines - 1) part = part.substring(0, max(1, static_cast<int>(part.length()) - 1)) + "...";
    centered(part, x, y + line * lineGap, color, size);
    rest = rest.substring(cut);
    rest.trim();
    ++line;
  }
}

String shortText(const char* value, size_t maxLength = 26) {
  String result = value ? String(value) : String();
  if (result.length() > maxLength) result = result.substring(0, maxLength - 1) + "...";
  return result;
}

String tailText(const String& value, size_t maxLength = 300) {
  if (value.length() <= maxLength) return value;
  return String("...") + value.substring(value.length() - maxLength + 3);
}

uint32_t statusColor(const char* status) {
  if (!status) return 0x657180;
  if (strcmp(status, "thinking") == 0) return 0x3489F4;
  if (strcmp(status, "complete") == 0) return 0x28C96D;
  if (strcmp(status, "needs_input") == 0) return 0xF5AB41;
  if (strcmp(status, "error") == 0) return 0xF05B69;
  return 0xAAB8C5;
}

const char* statusLabel(const char* status) {
  if (!status) return "IDLE";
  if (strcmp(status, "thinking") == 0) return "RUN";
  if (strcmp(status, "complete") == 0) return "DONE";
  if (strcmp(status, "needs_input") == 0) return "INPUT";
  if (strcmp(status, "error") == 0) return "ERROR";
  return "IDLE";
}

void drawPanel() {
  canvas.fillScreen(rgb(kBackground));
  canvas.fillSmoothCircle(kCenterX, kCenterY, kUiRadius, rgb(kPanel));
}

void drawWifiStatusIcon(int x, int y, uint16_t color) {
  canvas.drawArc(x, y + 8, 18, 18, 220, 320, color);
  canvas.drawArc(x, y + 8, 11, 11, 220, 320, color);
  canvas.fillCircle(x, y + 8, 3, color);
}

void drawBluetoothStatusIcon(int x, int y, uint16_t color) {
  canvas.drawLine(x, y - 13, x, y + 13, color);
  canvas.drawLine(x, y - 13, x + 8, y - 5, color);
  canvas.drawLine(x + 8, y - 5, x - 7, y + 7, color);
  canvas.drawLine(x - 7, y - 7, x + 8, y + 5, color);
  canvas.drawLine(x + 8, y + 5, x, y + 13, color);
}

void drawBatteryStatusIcon(int x, int y, uint16_t color) {
  canvas.drawRoundRect(x, y - 8, 26, 16, 3, color);
  canvas.fillRect(x + 27, y - 4, 4, 8, color);
  if (batteryLevel > 0) {
    const int fill = max(2, 18 * static_cast<int>(batteryLevel) / 100);
    canvas.fillRoundRect(x + 3, y - 5, fill, 10, 2, color);
  }
}

void drawStatusBar() {
  const uint32_t wifiColor = WiFi.status() == WL_CONNECTED ? 0xB8C7D4 : 0xF05B69;
  const uint32_t batteryColor = batteryLevel < 0 ? 0x657180
                                : (batteryLevel <= 15 ? 0xF05B69 : 0xB8C7D4);
  // This is a top overlay, shaped like a shallow watch capsule. It is
  // deliberately drawn after the page so the page keeps its full-screen
  // geometry and the capsule simply covers the upper part of it.
  const int barLeft = 113;
  const int barTop = -18;
  const int barWidth = 240;
  const int barHeight = 76;
  canvas.fillRoundRect(barLeft, barTop, barWidth, barHeight, 38, rgb(0x091016));
  canvas.drawRoundRect(barLeft, barTop, barWidth, barHeight, 38, rgb(0x3A4B57));
  drawWifiStatusIcon(165, 20, rgb(wifiColor));
  centered(batteryLevel < 0 ? "--" : String(static_cast<int>(batteryLevel)) + "%",
           kCenterX, 28, rgb(batteryColor), 1.35f);
  drawBatteryStatusIcon(274, 28, rgb(batteryColor));
}

void maskCircle() {
  const uint16_t outside = rgb(kBackground);
  for (int y = 0; y < kWidth; ++y) {
    const int dy = y - kCenterY;
    if (abs(dy) >= kUiRadius) {
      canvas.fillRect(0, y, kWidth, 1, outside);
      continue;
    }
    const int span = static_cast<int>(sqrtf(static_cast<float>(kUiRadius * kUiRadius - dy * dy)));
    const int left = kCenterX - span;
    const int right = kCenterX + span;
    if (left > 0) canvas.fillRect(0, y, left, 1, outside);
    if (right < kWidth) canvas.fillRect(right, y, kWidth - right, 1, outside);
  }
}

void maskStatusRows() {
  const uint16_t outside = rgb(kBackground);
  for (int y = 0; y < 76; ++y) {
    const int dy = y - kCenterY;
    if (abs(dy) >= kUiRadius) {
      canvas.fillRect(0, y, kWidth, 1, outside);
      continue;
    }
    const int span = static_cast<int>(sqrtf(static_cast<float>(kUiRadius * kUiRadius - dy * dy)));
    const int left = kCenterX - span;
    const int right = kCenterX + span;
    if (left > 0) canvas.fillRect(0, y, left, 1, outside);
    if (right < kWidth) canvas.fillRect(right, y, kWidth - right, 1, outside);
  }
}

void arcProgressAt(int centerY, int radius, int thickness, int percent, uint32_t color) {
  canvas.fillArc(kCenterX, centerY, radius, radius - thickness, 0, 360, rgb(0x26323D));
  if (percent > 0) canvas.fillArc(kCenterX, centerY, radius, radius - thickness, 0,
                                  max(1, min(360, percent * 36 / 10)), rgb(color));
}

void drawArrow(int x, int y, int direction, uint16_t color) {
  if (direction < 0) {
    canvas.drawLine(x + 9, y - 10, x - 7, y, color);
    canvas.drawLine(x - 7, y, x + 9, y + 10, color);
  } else {
    canvas.drawLine(x - 9, y - 10, x + 7, y, color);
    canvas.drawLine(x + 7, y, x - 9, y + 10, color);
  }
}

void drawTasksIcon(int x, int y, uint16_t color) {
  canvas.drawRoundRect(x - 15, y - 18, 30, 36, 5, color);
  for (int row = 0; row < 3; ++row) {
    const int yy = y - 7 + row * 9;
    canvas.drawLine(x - 9, yy, x - 3, yy, color);
    canvas.drawLine(x, yy, x + 9, yy, color);
  }
}

void drawApprovalIcon(int x, int y, uint16_t color) {
  canvas.drawCircle(x, y, 18, color);
  canvas.drawLine(x, y - 10, x, y + 4, color);
  canvas.fillCircle(x, y + 11, 2, color);
}

void drawWarningIcon(int x, int y, uint16_t color) {
  canvas.fillTriangle(x, y - 21, x - 23, y + 17, x + 23, y + 17, color);
  canvas.fillRoundRect(x - 3, y - 10, 6, 16, 2, rgb(0x10202A));
  canvas.fillCircle(x, y + 11, 3, rgb(0x10202A));
}

void fillArcSegment(int start, int end, int outer, int inner, uint16_t color) {
  while (start < 0) { start += 360; end += 360; }
  while (start >= 360) { start -= 360; end -= 360; }
  if (end <= 360) {
    canvas.fillArc(kCenterX, kCenterY, outer, inner, start, end, color);
  } else {
    canvas.fillArc(kCenterX, kCenterY, outer, inner, start, 360, color);
    canvas.fillArc(kCenterX, kCenterY, outer, inner, 0, end - 360, color);
  }
}

void drawSector(int index, int count, uint32_t color, int outer = kSectorOuter, int inner = kSectorInner) {
  const int size = 360 / count;
  const int gap = count == 1 ? 0 : 2;
  fillArcSegment(270 + index * size + gap, 270 + (index + 1) * size - gap, outer, inner, rgb(color));
}

void sectorPoint(int index, int count, int radius, int& x, int& y) {
  const float degrees = -90.0f + (index + 0.5f) * (360.0f / count);
  const float radians = degrees * 0.0174532925f;
  x = kCenterX + static_cast<int>(cosf(radians) * radius);
  y = kCenterY + static_cast<int>(sinf(radians) * radius);
}

int sectorAt(int x, int y, int count, int outer = kSectorOuter, int inner = kSectorInner) {
  const int dx = x - kCenterX;
  const int dy = y - kCenterY;
  const int distance = static_cast<int>(sqrtf(static_cast<float>(dx * dx + dy * dy)));
  if (distance < inner || distance > outer) return -1;
  float degrees = atan2f(static_cast<float>(dy), static_cast<float>(dx)) * 57.2957795f + 90.0f;
  if (degrees < 0) degrees += 360.0f;
  if (degrees >= 360.0f) degrees -= 360.0f;
  return static_cast<int>(degrees / (360.0f / count)) % count;
}

void drawHome(JsonObject doc) {
  JsonObject codex = doc["codex"].as<JsonObject>();
  JsonObject usage = doc["usage"].as<JsonObject>();
  const bool connected = codex["connected"] | false;
  const bool hasDailyTokens = !usage["daily_tokens_display"].isNull() &&
                              String(usage["daily_tokens_display"] | "").length() > 0;
  const String dailyTokens = hasDailyTokens ? String(usage["daily_tokens_display"] | "") : String();
  const bool hasUsage = !usage["remaining_percent"].isNull();
  const int remaining = hasUsage ? static_cast<int>(usage["remaining_percent"].as<float>()) : -1;
  const int agents = doc["agent_count"] | 0;
  const bool pending = doc["interactions"].size() > 0;
  JsonArray agentList = doc["agents"].as<JsonArray>();
  const uint32_t accent = !connected ? 0xF05B69 : 0x22D9E3;
  const bool pulse = pending && ((millis() / 450) % 2 == 0);

  centered("ACTIVE CODEX SESSION", kCenterX, 100, rgb(0xD4DEE7), 1.55f);
  canvas.drawLine(134, 111, 134, 354, rgb(0x41515F));
  canvas.drawLine(332, 111, 332, 354, rgb(0x41515F));

  centered("SESSION", 84, 135, rgb(0xB9C5D0), 1.3f);
  centered("ACTIONS", 84, 153, rgb(0xB9C5D0), 1.3f);
  centered("APPROVALS", 382, 144, rgb(0xB9C5D0), 1.3f);

  constexpr int homeCenterY = 246;
  arcProgressAt(homeCenterY, 96, 10, hasDailyTokens ? 0 : (hasUsage ? remaining : 0), accent);
  canvas.fillSmoothCircle(kCenterX, homeCenterY, 79, rgb(0x1A2A35));
  centered(connected ? "CODEX LIVE" : "CODEX OFFLINE", kCenterX, 196, rgb(accent), 1.55f);
  if (hasDailyTokens) {
    centered("TOKENS TODAY", kCenterX, 224, rgb(0x9FE7EC), 1.45f);
    centered(dailyTokens, kCenterX, 266, rgb(0x5BE7EE), 3.55f);
  } else {
    centered(hasUsage ? String(remaining) + "%" : "--", kCenterX, 245, rgb(0x5BE7EE), 3.55f);
    centered("SESSION IN", kCenterX, 288, rgb(0x9FE7EC), 1.55f);
    centered("PROGRESS", kCenterX, 310, rgb(0x9FE7EC), 1.55f);
  }

  canvas.fillRoundRect(38, 177, 92, 145, 14, rgb(0x22364A));
  drawTasksIcon(84, 214, rgb(0x5BE7EE));
  centered("MANAGE", 84, 261, rgb(0xF1F6FA), 1.25f);
  centered("TASKS", 84, 283, rgb(0xF1F6FA), 1.25f);
  centered("(D/A/C/B)", 84, 305, rgb(0xD4DEE7), 1.15f);

  const uint32_t approvalFill = pending && pulse ? 0x6B3B38 : (pending ? 0x4A3032 : 0x202B34);
  canvas.fillRoundRect(336, 177, 92, 145, 14, rgb(approvalFill));
  drawWarningIcon(382, 214, rgb(pending ? 0x32DDE8 : 0x657180));
  centered("APPROVAL", 382, 261, rgb(pending ? 0xF1F6FA : 0x7C8995), 1.2f);
  canvas.drawRoundRect(344, 279, 76, 32, 12, rgb(pending ? 0x43C8D7 : 0x52616D));
  centered(pending ? "OPEN" : "NONE", 382, 295, rgb(pending ? 0xFFF3D5 : 0x9BA8B2), 1.2f);

  canvas.drawLine(62, 365, 404, 365, rgb(0x41515F));
  for (int i = 0; i < min(kAgentCount, static_cast<int>(agentList.size())); ++i) {
    JsonObject agent = agentList[i].as<JsonObject>();
    const int row = i / 2;
    const int col = i % 2;
    const int chipY = 348 + row * 25;
    const int chipX = row == 0 ? (col == 0 ? 78 : 268)
                               : (row == 1 ? (col == 0 ? 91 : 255)
                                           : (col == 0 ? 121 : 245));
    const uint32_t color = statusColor(agent["status"] | "idle");
    const int chipWidth = row < 2 ? 120 : 100;
    canvas.fillRoundRect(chipX, chipY, chipWidth, 21, 9, rgb(0x243444));
    canvas.fillCircle(chipX + 11, chipY + 10, 5, rgb(color));
    text(String("A") + String(i + 1) + ": " + statusLabel(agent["status"] | "idle"),
         chipX + 21, chipY + 10, rgb(0xE8EEF5), 1.25f);
  }
}

void drawTasks(JsonObject doc) {
  JsonArray list = doc["agents"].as<JsonArray>();
  centered("TASKS", kCenterX, 94, rgb(0xE8EEF5), 2.2f);
  const int visible = min(kAgentCount, static_cast<int>(list.size()));
  for (int i = 0; i < visible; ++i) {
    JsonObject agent = list[i].as<JsonObject>();
    const int y = 126 + i * 38;
    const uint32_t color = statusColor(agent["status"] | "idle");
    canvas.fillRoundRect(52, y, 362, 34, 12, rgb(0x1B2933));
    canvas.fillCircle(71, y + 17, 7, rgb(color));
    text(String("A") + String(i + 1), 90, y + 17, rgb(0xE8EEF5), 1.55f);
    text(statusLabel(agent["status"] | "idle"), 145, y + 17, rgb(color), 1.55f);
    text(shortText(agent["title"] | "Task", 18), 218, y + 17, rgb(0xB9C5D0), 1.55f);
  }
  if (visible == 0) centered("NO ACTIVE TASKS", kCenterX, 250, rgb(0x91A2B2), 1.9f);
  drawArrow(32, 94, -1, rgb(0x91A2B2));
}

void drawApproval(JsonObject doc) {
  JsonArray list = doc["interactions"].as<JsonArray>();
  if (list.isNull() || list.size() == 0) {
    canvas.fillSmoothCircle(kCenterX, kCenterY, 106, rgb(0x18242D));
    canvas.drawCircle(kCenterX, kCenterY, 106, rgb(0x3A4A55));
    centered("NO PENDING", kCenterX, 226, rgb(0x63DCC5), 2.1f);
    centered("INTERACTION", kCenterX, 268, rgb(0x91A2B2), 1.75f);
    return;
  }
  JsonObject item = list[0].as<JsonObject>();
  JsonArray options = item["options"].as<JsonArray>();
  const String kind = item["kind"] | "choice";
  const bool approval = kind == "approval";
  const int optionCount = approval ? 2 : max(2, min(4, static_cast<int>(options.size())));
  for (int i = 0; i < optionCount; ++i) {
    const uint32_t fill = approval ? (i == 0 ? 0x164238 : 0x482832) : 0x1A4050;
    drawSector(i, optionCount, fill);
    int x = 0;
    int y = 0;
    sectorPoint(i, optionCount, 146, x, y);
    const String label = approval ? (i == 0 ? "ALLOW" : "DENY")
                                  : (i < static_cast<int>(options.size()) ? shortText(options[i] | "", 16) : "EMPTY");
    centered(String(char('A' + i)), x, y - 26, rgb(0x63DCC5), 1.9f);
    centeredWrapped(label, x, y + 5, approval ? 8 : 11, rgb(0xF1F6FA), 1.75f, 23, 2);
  }
  canvas.fillSmoothCircle(kCenterX, kCenterY, kSectorInner - 6, rgb(0x070B10));
  centeredWrapped(shortText(item["prompt"] | "Input required", 42), kCenterX, 207, 12,
                  rgb(0xFFF3D5), 1.55f, 21, 3);
}

uint32_t agentSectorColor(int index) {
  static const uint32_t colors[kAgentSlots] = {
      0xE53935, 0xF28C28, 0xF4D03F, 0x2ECC71, 0x16C6C1, 0x3478DB, 0x8E44AD};
  return colors[index % kAgentSlots];
}

uint16_t agentSectorTextColor(int index) {
  return rgb((index == 2 || index == 3 || index == 4) ? 0x10151B : 0xFFFFFF);
}

void drawAgents(JsonObject doc) {
  JsonArray list = doc["agents"].as<JsonArray>();
  // The server's agent array is the source of truth. Do not render empty
  // placeholders: a one-agent view is one complete ring, two agents are two
  // half-rings, and so on.
  const int count = min(kAgentSlots, static_cast<int>(list.size()));
  for (int i = 0; i < count; ++i) {
    drawSector(i, count, agentSectorColor(i));
  }
  canvas.fillSmoothCircle(kCenterX, kCenterY, kSectorInner - 6, rgb(0x070B10));
  for (int i = 0; i < count; ++i) {
    int x = 0;
    int y = 0;
    sectorPoint(i, count, 151, x, y);
    JsonObject agent = list[i].as<JsonObject>();
    const char* label = statusLabel(agent["status"] | "idle");
    const uint16_t labelColor = agentSectorTextColor(i);
    centered(String("A") + String(i + 1), x, y - 15, labelColor, 1.75f);
    centered(label, x, y + 14, labelColor, 1.45f);
  }
  centered("ACTIVE " + String(count), kCenterX, kCenterY + 7, rgb(0x63DCC5), 2.0f);
}

void drawAgentDetail(JsonObject doc) {
  JsonArray list = doc["agents"].as<JsonArray>();
  JsonObject selected;
  int selectedIndex = -1;
  for (int i = 0; i < static_cast<int>(list.size()); ++i) {
    JsonObject candidate = list[i].as<JsonObject>();
    if (String(candidate["id"] | "") == selectedAgentId) {
      selected = candidate;
      selectedIndex = i;
      break;
    }
  }
  if (selected.isNull()) {
    centered("AGENT NOT FOUND", kCenterX, 226, rgb(0xF5AB41), 1.8f);
    return;
  }

  const char* status = selected["status"] | "idle";
  const uint32_t accent = agentSectorColor(selectedIndex < 0 ? 0 : selectedIndex);
  const String title = String(selected["title"] | "Codex agent");
  const String output = tailText(String(selected["output"] | ""));
  const String model = String(selected["model"] | "");
  const String cwd = String(selected["cwd"] | "");

  centeredWrapped(title, kCenterX, 86, 25, rgb(0xF1F6FA), 1.75f, 25, 2);
  canvas.fillRoundRect(166, 119, 134, 28, 12, rgb(accent));
  centered(statusLabel(status), kCenterX, 133, agentSectorTextColor(selectedIndex < 0 ? 0 : selectedIndex), 1.25f);

  String metadata;
  if (model.length()) metadata = model;
  if (cwd.length()) {
    if (metadata.length()) metadata += "  |  ";
    metadata += shortText(cwd.c_str(), 28);
  }
  if (metadata.length()) centeredWrapped(metadata, kCenterX, 162, 29, rgb(0x9FB1BE), 1.25f, 19, 2);

  canvas.drawLine(82, 194, 384, 194, rgb(0x354752));
  centered("LATEST OUTPUT", kCenterX, 214, rgb(0x63DCC5), 1.35f);
  if (output.length()) {
    centeredWrapped(output, kCenterX, 247, 27, rgb(0xE6EEF3), 1.45f, 24, 7);
  } else {
    centered("WAITING FOR OUTPUT", kCenterX, 270, rgb(0x91A2B2), 1.55f);
  }
}

bool parseState(DynamicJsonDocument& doc) {
  return deserializeJson(doc, stateJson) == DeserializationError::Ok;
}

void renderPage(View view, M5Canvas* target, DynamicJsonDocument& doc) {
  activeCanvas = target;
  // A page is a complete frame, never an incremental overlay. Clear the
  // sprite before drawing so content from a previous view cannot survive a
  // page change or an interrupted transition.
  canvas.fillScreen(rgb(kBackground));
  drawPanel();
  if (parseState(doc)) {
    JsonObject root = doc.as<JsonObject>();
    switch (view) {
      case View::Home: drawHome(root); break;
      case View::Tasks: drawTasks(root); break;
      case View::Approval: drawApproval(root); break;
      case View::Agents: drawAgents(root); break;
      case View::AgentDetail: drawAgentDetail(root); break;
    }
  } else {
    centered(WiFi.status() == WL_CONNECTED ? "WAITING FOR SERVER" : "CONNECTING WIFI",
             kCenterX, 230, rgb(0xF5AB41), 1.9f);
  }
  // Every page owns its status bar. Keeping it in the same opaque frame means
  // a page can never expose pixels left by the previous view.
  drawStatusBar();
  maskCircle();
}

void renderView(View view, M5Canvas* target, DynamicJsonDocument& doc) {
  renderPage(view, target, doc);
}

void drawStatusBarOn(M5Canvas* target) {
  M5Canvas* previous = activeCanvas;
  activeCanvas = target;
  drawStatusBar();
  activeCanvas = previous;
}

void drawStatusBarOnly() {
  statusCanvas.fillScreen(rgb(kBackground));
  drawStatusBarOn(&statusCanvas);
  M5Canvas* previous = activeCanvas;
  activeCanvas = &statusCanvas;
  maskStatusRows();
  activeCanvas = previous;
  statusCanvas.pushSprite(0, 0, statusCanvas.color565((kBackground >> 16) & 255,
                                                       (kBackground >> 8) & 255,
                                                       kBackground & 255));
}

void clearDisplay() {
  // The panel is a physical framebuffer. Clear it before every complete frame
  // so pixels from the previous page cannot survive a page replacement.
  M5.Display.fillScreen(M5.Display.color565((kBackground >> 16) & 255,
                                             (kBackground >> 8) & 255,
                                             kBackground & 255));
}

void drawScreen(const char* banner = nullptr) {
  (void)banner;
  DynamicJsonDocument doc(kJsonCapacity);
  renderView(currentView, &mainCanvas, doc);
  activeCanvas = &mainCanvas;
  mainCanvas.pushSprite(0, 0);
}

float easeOutCubic(float value) {
  const float inverse = 1.0f - value;
  return 1.0f - inverse * inverse * inverse;
}

void animateTo(View next, SlideFrom incomingSide) {
  if (next == currentView) return;
  DynamicJsonDocument doc(kJsonCapacity);
  // Keep both complete pages in sprites. This is the same double-buffering
  // model used by the official graphics examples: the display only receives
  // finished frames, never a half-drawn page.
  renderView(currentView, &mainCanvas, doc);
  renderView(next, &transitionCanvas, doc);
  constexpr int frames = 18;
  for (int frame = 1; frame <= frames; ++frame) {
    const float progress = easeOutCubic(static_cast<float>(frame) / frames);
    const int offset = static_cast<int>(kWidth * progress);
    int oldX = 0;
    int oldY = 0;
    int newX = 0;
    int newY = 0;
    switch (incomingSide) {
      case SlideFrom::Left:
        oldX = offset;
        newX = -kWidth + offset;
        break;
      case SlideFrom::Right:
        oldX = -offset;
        newX = kWidth - offset;
        break;
      case SlideFrom::Top:
        oldY = offset;
        newY = -kWidth + offset;
        break;
      case SlideFrom::Bottom:
        oldY = -offset;
        newY = kWidth - offset;
        break;
    }
    // The two opaque sprites exactly cover the moving viewport. Keeping both
    // in the same frame avoids the black flash caused by clearing the panel.
    mainCanvas.pushSprite(oldX, oldY);
    transitionCanvas.pushSprite(newX, newY);
    delay(14);
  }
  currentView = next;
  drawScreen();
  // A page transition is terminal for its originating gesture. This prevents
  // a delayed touch event from being interpreted by the destination page.
  discardNavigationTouch(kTransitionTouchGuardMs);
}

String normalizeStateForDisplay(const String& raw) {
  DynamicJsonDocument doc(kJsonCapacity);
  if (deserializeJson(doc, raw) != DeserializationError::Ok) return raw;
  doc.remove("version");
  doc.remove("server_time");
  doc.remove("last_event");
  doc.remove("last_event_at");
  JsonObject usage = doc["usage"].as<JsonObject>();
  if (!usage.isNull()) {
    usage.remove("updated_at");
    usage.remove("reset_in_seconds");
    usage.remove("error");
  }
  JsonArray agents = doc["agents"].as<JsonArray>();
  for (JsonObject agent : agents) {
    agent.remove("updated_at");
    agent.remove("source_updated_at");
  }
  JsonArray interactions = doc["interactions"].as<JsonArray>();
  for (JsonObject interaction : interactions) interaction.remove("created_at");
  String normalized;
  serializeJson(doc, normalized);
  return normalized;
}

bool postAction(const String& body) {
  if (WiFi.status() != WL_CONNECTED) return false;
  HTTPClient http;
  http.begin(apiUrl("/api/v1/actions"));
  http.addHeader("Content-Type", "application/json");
  http.addHeader("X-Codex-Touch-Token", TOUCH_SERVER_TOKEN);
  const int code = http.POST(body);
  http.end();
  return code >= 200 && code < 300;
}

bool answerPending(JsonObject interaction, const char* action, int choice = -1) {
  const char* requestId = interaction["request_id"] | "";
  if (!requestId[0]) return false;
  StaticJsonDocument<256> doc;
  doc["type"] = action;
  doc["request_id"] = requestId;
  if (choice >= 0) {
    JsonArray answers = doc.createNestedArray("answers");
    answers.add(choice);
  }
  String body;
  serializeJson(doc, body);
  return postAction(body);
}

void pollState() {
  if (WiFi.status() != WL_CONNECTED) {
    const bool changed = serverReachable;
    serverReachable = false;
    serverLatencyMs = 0;
    if (changed) drawScreen();
    return;
  }
  HTTPClient http;
  http.begin(apiUrl("/api/v1/state"));
  http.addHeader("X-Codex-Touch-Token", TOUCH_SERVER_TOKEN);
  const uint32_t started = millis();
  const int code = http.GET();
  const bool previousReachable = serverReachable;
  serverLatencyMs = millis() - started;
  serverReachable = code == HTTP_CODE_OK;
  bool shouldRedraw = previousReachable != serverReachable;
  if (code == HTTP_CODE_OK) {
    const String nextState = normalizeStateForDisplay(http.getString());
    shouldRedraw = shouldRedraw || stateJson != nextState;
    stateJson = nextState;
  }
  http.end();
  if (shouldRedraw) drawScreen();
}

int wifiPercent() {
  if (WiFi.status() != WL_CONNECTED) return 0;
  return constrain((WiFi.RSSI() + 90) * 100 / 50, 0, 100);
}

void pollTelemetry() {
  if (millis() - lastTelemetryPoll < kTelemetryMs) return;
  lastTelemetryPoll = millis();
  const int8_t oldBattery = batteryLevel;
  const int oldWifi = wifiLevel;
  const bool oldServer = serverReachable;
  batteryLevel = axpReady ? axp::batteryPercent() : -1;
  wifiLevel = wifiPercent();
  if (WiFi.status() != WL_CONNECTED) serverReachable = false;
  const bool statusChanged = batteryLevel != oldBattery || wifiLevel != oldWifi;
  const bool serverChanged = serverReachable != oldServer;
  if (serverChanged) drawScreen();
  else if (statusChanged) drawStatusBarOnly();
}

void goHome() {
  if (currentView == View::Tasks) animateTo(View::Home, SlideFrom::Right);
  else if (currentView == View::Approval) animateTo(View::Home, SlideFrom::Left);
  else if (currentView == View::Agents) animateTo(View::Home, SlideFrom::Top);
  else if (currentView == View::AgentDetail) animateTo(View::Home, SlideFrom::Right);
}

void discardNavigationTouch(uint32_t durationMs) {
  touching = false;
  touchStartX = 0;
  touchStartY = 0;
  const uint32_t requestedUntil = millis() + durationMs;
  if (static_cast<int32_t>(requestedUntil - ignoreTouchUntil) > 0) {
    ignoreTouchUntil = requestedUntil;
  }
  requireFreshTouch = true;
}

void handleTouch() {
  const auto touch = M5.Touch.getDetail();
  const int x = (232 - touch.x) * 2;
  const int y = (232 - touch.y) * 2;
  if (static_cast<int32_t>(millis() - ignoreTouchUntil) < 0) {
    touching = false;
    return;
  }
  if (requireFreshTouch) {
    touching = false;
    if (!touch.wasPressed()) return;
    requireFreshTouch = false;
    touching = true;
    touchStartX = x;
    touchStartY = y;
    return;
  }
  if (touch.wasPressed()) {
    touching = true;
    touchStartX = x;
    touchStartY = y;
  }
  if (!touch.wasReleased() || !touching) return;
  touching = false;
  const int dx = x - touchStartX;
  const int dy = y - touchStartY;
  if (abs(dx) > kSwipeThreshold || abs(dy) > kSwipeThreshold) {
    if (abs(dx) >= abs(dy)) {
      if (dx > 0 && currentView == View::Home) animateTo(View::Tasks, SlideFrom::Left);
      else if (dx < 0 && currentView == View::Home) animateTo(View::Approval, SlideFrom::Right);
      else if (dx < 0 && currentView == View::Tasks) animateTo(View::Home, SlideFrom::Right);
      else if (dx > 0 && currentView == View::Approval) animateTo(View::Home, SlideFrom::Left);
    } else if (dy < 0 && currentView == View::Home) {
      animateTo(View::Agents, SlideFrom::Bottom);
    } else if (dy > 0 && currentView == View::Agents) {
      animateTo(View::Home, SlideFrom::Top);
    } else if (dy > 0 && currentView == View::AgentDetail) {
      animateTo(View::Agents, SlideFrom::Top);
    }
    return;
  }

  DynamicJsonDocument current(kJsonCapacity);
  if (!parseState(current)) return;
  if (currentView == View::Home) {
    const bool pending = current["interactions"].size() > 0;
    if (x >= 38 && x <= 130 && y >= 177 && y <= 322) {
      animateTo(View::Tasks, SlideFrom::Left);
      return;
    }
    if (pending && x >= 336 && x <= 428 && y >= 177 && y <= 322) {
      animateTo(View::Approval, SlideFrom::Right);
      return;
    }
    if (y >= 350 && y <= 430 && abs(x - kCenterX) < 190) {
      animateTo(View::Agents, SlideFrom::Bottom);
      return;
    }
  }
  if (currentView == View::Approval) {
    JsonArray pending = current["interactions"].as<JsonArray>();
    if (!pending.isNull() && pending.size() > 0) {
      JsonObject interaction = pending[0].as<JsonObject>();
      const String kind = interaction["kind"] | "choice";
      const int optionCount = kind == "approval" ? 2 : max(2, min(4, static_cast<int>(interaction["options"].size())));
      const int choice = sectorAt(x, y, optionCount);
      if (choice >= 0) {
        if (kind == "approval") answerPending(interaction, choice == 0 ? "approve" : "deny");
        else answerPending(interaction, "choose", choice);
        drawScreen();
      }
    }
  }
  if (currentView == View::Agents) {
    JsonArray agents = current["agents"].as<JsonArray>();
    const int count = min(kAgentSlots, static_cast<int>(agents.size()));
    const int index = count > 0 ? sectorAt(x, y, count) : -1;
    if (index >= 0 && index < count) {
      JsonObject agent = agents[index].as<JsonObject>();
      selectedAgentId = String(agent["id"] | "");
      if (selectedAgentId.length()) {
        postAction(String("{\"type\":\"select_agent\",\"agent_id\":\"") + selectedAgentId + "\"}");
        animateTo(View::AgentDetail, SlideFrom::Bottom);
      }
    }
  }
}

bool updatePowerButton() {
  if (!axpReady || millis() - lastPowerPoll < 30) return false;
  lastPowerPoll = millis();
  const uint8_t press = axp::pekPress();
  if (press & 0x02) {
    // Power-key input is exclusive. Do not allow a stale touchscreen release
    // from the same event loop to activate a Home-page navigation target.
    discardNavigationTouch(kPowerTouchGuardMs);
    Serial.println("PWR short press: HOME (touch guarded)");
    goHome();
    return true;
  }
  return false;
}
}  // namespace

void setup() {
  auto config = M5.config();
  // Waveshare is rendered through M5StopWatch support, whose PMIC key is
  // M5PM1. This board uses AXP2101 and is handled explicitly below.
  config.pmic_button = false;
  M5.begin(config);
  mainCanvas.createSprite(kWidth, kWidth);
  transitionCanvas.createSprite(kWidth, kWidth);
  statusCanvas.createSprite(kWidth, 76);
  mainCanvas.setColorDepth(16);
  transitionCanvas.setColorDepth(16);
  statusCanvas.setColorDepth(16);
  axpReady = axp::init();
  drawScreen("CONNECTING");
  WiFi.mode(WIFI_STA);
  WiFi.begin(TOUCH_WIFI_SSID, TOUCH_WIFI_PASSWORD);
  configTime(8 * 3600, 0, "pool.ntp.org", "time.nist.gov");
  Serial.printf("CODEX_TOUCH_READY AXP=%d\n", axpReady ? 1 : 0);
}

void loop() {
  M5.update();
  if (!updatePowerButton()) handleTouch();
  pollTelemetry();
  const uint32_t now = millis();
  if (WiFi.status() != WL_CONNECTED && now - lastWifiAttempt >= kWifiRetryMs) {
    lastWifiAttempt = now;
    WiFi.disconnect();
    WiFi.begin(TOUCH_WIFI_SSID, TOUCH_WIFI_PASSWORD);
    drawScreen("WIFI RETRY");
  }
  if (now - lastPoll >= kPollMs) {
    lastPoll = now;
    pollState();
  }
  delay(10);
}

// ===========================================================================
// obd_ble.h — Bluetooth-LE OBD-II (ELM327) client for the CrowPanel dash.
//
// Purpose: when the sensor source is "Bluetooth" (sensor_type == 2), talk to a
// BLE ELM327 OBD-II dongle plugged into the car's OBD-II port and read SLOW
// values (coolant temp, intake-air temp, module voltage). NOT for RPM — RPM is
// live/time-critical and stays on the opto tach / MS3 CAN via the Teensy.
//
// HARD RULE (learned from the GPS-stale saga): BLE calls block for seconds
// (connect, service discovery, waiting for ELM responses). We therefore run
// ALL of it on a DEDICATED FreeRTOS task pinned to core 0 — it can block all it
// wants without ever touching the 60fps dash loop on core 1. The UI only sets
// request flags (scan / connect) and reads plain volatile values.
//
// The dongle's GATT layout varies by brand (0xFFF0/0xFFE0/vendor UUIDs), so we
// AUTO-DISCOVER: after connecting we walk every service/characteristic and pick
// the first that can notify (RX) and the first that can write (TX). Works across
// the common "Vgate iCar Pro BLE", "Viecar", generic FFE0/FFF0 clones, etc.
//
// ELM327 flow: ATZ, ATE0, ATL0, ATS0, ATH0, ATSP0 then poll PIDs round-robin.
//
// NimBLE version: **1.4.x** (NOT 2.x). 2.x targets the arduino-esp32 3.x core
// (IDF 5); on this board's 2.0.14 core (IDF 4.4) it compiles but crash-reboots
// on BLE init. 1.4.3 is the battle-tested match for 2.0.14. API differs from 2.x
// (scan start() returns results in SECONDS, getDevice() by value, getServices/
// getCharacteristics return POINTERS, onDisconnect has no reason arg).
// ===========================================================================
#pragma once
#include <Arduino.h>
#include <NimBLEDevice.h>
#include <esp_heap_caps.h>

namespace obd {

enum State : uint8_t {
  OFF = 0,      // not running (BT source not selected)
  IDLE,         // NimBLE up, nothing to do
  SCANNING,     // BLE scan in progress
  CONNECTING,   // opening GATT connection
  DISCOVER,     // finding TX/RX characteristics
  INIT,         // sending ELM327 AT setup
  POLL,         // connected + polling PIDs
  RECONNECT,    // lost link, waiting to retry
  FAILED        // connect/discover failed (will retry)
};

struct ScanItem { char name[24]; char addr[18]; uint8_t atype; int8_t rssi; };

// ---- shared state (task writes, UI reads; scalars = lock-free enough) -------
static volatile State    g_state   = OFF;
static bool              g_inited  = false;   // NimBLE + task started
static bool              g_blocked = false;   // app sets this if a prior BLE init crashed the board
static TaskHandle_t      g_task    = nullptr;

// Crash-forensics PHASE breadcrumb (NVS "blediag"/"ph"): records which BLE
// phase is active so a crash during ANY of them is attributable on next boot:
//   0=inactive  1=init  2=scan  3=connect/discover/ELM-init  4=connected
// The v0.1.92 breadcrumb only covered init and was cleared right after — a
// crash during SCAN (which is when the radio really starts working) left no
// trace and didn't block the next auto-init. Written only on state
// transitions (a handful of NVS writes per session, wear-safe). The boot-side
// reader cross-checks esp_reset_reason() and only attributes crashy reasons
// (brownout/panic/wdt) — a normal power-off mid-session leaves the phase set
// but reads as POWERON and is cleared silently.
static void blePhase(uint8_t ph) {
  Preferences p;
  if (p.begin("blediag", false)) { p.putUChar("ph", ph); p.end(); }
}

// last human-readable BLE error (shown on the Sensor page)
static char g_last_err[48] = {0};
static const char* lastErr() { return g_last_err; }

// Last raw ELM reply snippet (shown on the Sensor page when coolant is
// missing — surfaces "NODATA" / "UNABLETOCONNECT" / "SEARCHING" so "connected
// but no ECU answer" (ignition off, dead OBD bus) is diagnosable on-screen).
static char g_last_resp[28] = {0};
static const char* lastResp() { return g_last_resp; }

// live data — sentinel -1 (or 0 for rpm) until a valid reading arrives
static volatile int16_t  d_coolant_f_x10 = -1;
static volatile int16_t  d_iat_f_x10     = -1;
static volatile int16_t  d_volt_x10      = -1;
static volatile uint32_t d_last_ms       = 0;   // millis() of last good PID parse

// scan results (populated by the task after a blocking scan completes)
static ScanItem          g_scan[16];
static volatile uint8_t  g_scan_n   = 0;
static volatile bool     g_scanning = false;

// UI -> task requests
static volatile bool     g_req_scan     = false;
static volatile bool     g_req_connect  = false;
static volatile bool     g_req_stop     = false;
static volatile bool     g_req_shutdown = false;   // FULL teardown incl. NimBLE deinit (radio handover to WiFi)

// chosen device (the one we (re)connect to)
static char              g_target_addr[18] = {0};
static volatile uint8_t  g_target_atype    = 0;
static char              g_conn_name[24]   = {0};   // friendly name of connected device

// GATT handles
static NimBLEClient*               g_client = nullptr;
static NimBLERemoteCharacteristic* g_tx     = nullptr;   // write (to ELM)
static NimBLERemoteCharacteristic* g_rx     = nullptr;   // notify (from ELM)

// RX assembly (notify runs on the NimBLE host task; parsed on the OBD task)
static portMUX_TYPE      g_mux = portMUX_INITIALIZER_UNLOCKED;
static char              g_rx_buf[256];
static volatile uint16_t g_rx_len = 0;
static volatile bool     g_prompt = false;    // ELM '>' prompt seen = reply done

// ---- helpers ---------------------------------------------------------------
static inline int hexNibble(char c) {
  if (c >= '0' && c <= '9') return c - '0';
  if (c >= 'A' && c <= 'F') return c - 'A' + 10;
  if (c >= 'a' && c <= 'f') return c - 'a' + 10;
  return -1;
}
// find PID echo (e.g. "4105") in the response and return the next byte (2 hex)
static int pidByte(const char* resp, const char* tag, int nthByte) {
  const char* p = strstr(resp, tag);
  if (!p) return -1;
  p += strlen(tag);
  p += nthByte * 2;
  int hi = hexNibble(p[0]); if (hi < 0) return -1;
  int lo = hexNibble(p[1]); if (lo < 0) return -1;
  return (hi << 4) | lo;
}

static void notifyCB(NimBLERemoteCharacteristic*, uint8_t* data, size_t len, bool) {
  portENTER_CRITICAL(&g_mux);
  for (size_t i = 0; i < len; i++) {
    char ch = (char)data[i];
    if (ch == '>') { g_prompt = true; continue; }
    if (ch == '\r' || ch == '\n' || ch == ' ') continue;   // ATS0 kills spaces anyway
    if (g_rx_len < sizeof(g_rx_buf) - 1) g_rx_buf[g_rx_len++] = ch;
  }
  portEXIT_CRITICAL(&g_mux);
}

// send one ELM command, wait (blocking on THIS task only) for the '>' prompt.
static bool elmCmd(const char* cmd, uint32_t timeout_ms) {
  if (!g_tx) return false;
  portENTER_CRITICAL(&g_mux); g_rx_len = 0; g_prompt = false; g_rx_buf[0] = 0; portEXIT_CRITICAL(&g_mux);
  char b[24];
  int n = snprintf(b, sizeof(b), "%s\r", cmd);
  // Use the write mode the characteristic supports: a write-WITH-response-only
  // char silently ignores ATT Write Commands (review fix).
  g_tx->writeValue((uint8_t*)b, n, !g_tx->canWriteNoResponse());
  uint32_t t0 = millis();
  while (!g_prompt && (millis() - t0) < timeout_ms) vTaskDelay(pdMS_TO_TICKS(10));
  return g_prompt;
}
// snapshot the accumulated response into a C string for parsing
static void snapResp(char* out, size_t outsz) {
  portENTER_CRITICAL(&g_mux);
  uint16_t n = g_rx_len; if (n > outsz - 1) n = outsz - 1;
  memcpy(out, g_rx_buf, n); out[n] = 0;
  portEXIT_CRITICAL(&g_mux);
}

// ---- client callbacks (NimBLE 1.4.x: onDisconnect has no reason arg) --------
class ClientCB : public NimBLEClientCallbacks {
  void onDisconnect(NimBLEClient*) override {
    g_tx = g_rx = nullptr;
    // only auto-reconnect if the user still wants BT (not a deliberate stop)
    if (g_state != OFF && !g_req_stop) g_state = RECONNECT;
  }
};
static ClientCB g_clientCB;

// ---- the worker task -------------------------------------------------------
static void doScan() {
  g_scanning = true; g_scan_n = 0;
  blePhase(2);
  NimBLEScan* sc = NimBLEDevice::getScan();
  // ACTIVE scan (v0.1.106): device NAMES mostly live in the SCAN RESPONSE,
  // which the scanner only receives if it actively requests it — the passive
  // scan (a leftover crash mitigation) listed most devices as "(unnamed)".
  // Safe now: the historical crash was the WiFi+BLE coex bug, and the radio
  // time-share keeps WiFi HARD-OFF during every scan, so BLE owns the radio
  // outright. SCAN_REQ TX stays at 0 dBm; ~45% duty is plenty gentle.
  sc->setActiveScan(true);
  sc->setInterval(100);   // ms
  sc->setWindow(45);      // ms of each interval actually listening
  sc->setMaxResults(24);
  sc->clearResults();
  NimBLEScanResults res = sc->start(8, false);   // 8 s, blocking on THIS task only
  uint8_t n = 0;
  for (int i = 0; i < res.getCount() && n < 16; i++) {
    NimBLEAdvertisedDevice d = res.getDevice(i);   // 1.4.x: by value
    std::string nm = d.getName();
    std::string ad = d.getAddress().toString();
    // keep named devices first-class; unnamed still listed by address
    strncpy(g_scan[n].name, nm.empty() ? "(unnamed)" : nm.c_str(), sizeof(g_scan[n].name) - 1);
    g_scan[n].name[sizeof(g_scan[n].name) - 1] = 0;
    strncpy(g_scan[n].addr, ad.c_str(), sizeof(g_scan[n].addr) - 1);
    g_scan[n].addr[sizeof(g_scan[n].addr) - 1] = 0;
    g_scan[n].atype = d.getAddressType();
    g_scan[n].rssi  = d.getRSSI();
    n++;
  }
  // Strongest signal first — the dongle IN the car tops the list (and named
  // devices win ties). Tiny N: insertion sort.
  for (uint8_t i = 1; i < n; i++) {
    ScanItem tmp = g_scan[i];
    int8_t j = i - 1;
    while (j >= 0 && g_scan[j].rssi < tmp.rssi) { g_scan[j + 1] = g_scan[j]; j--; }
    g_scan[j + 1] = tmp;
  }
  g_scan_n = n;
  g_scanning = false;
  blePhase(0);
}

static bool connectAndInit() {
  if (!g_target_addr[0]) return false;
  g_state = CONNECTING;
  blePhase(3);
  if (!g_client) {
    g_client = NimBLEDevice::createClient();
    g_client->setClientCallbacks(&g_clientCB, false);
  }
  NimBLEAddress addr(std::string(g_target_addr), g_target_atype);
  if (!g_client->isConnected() && !g_client->connect(addr)) { g_state = FAILED; blePhase(0); return false; }

  g_state = DISCOVER;
  g_tx = g_rx = nullptr;
  // REVIEW FIX: the old "first notify anywhere + first writable anywhere" pick
  // latched onto the STANDARD Generic-Attribute service's "Service Changed"
  // characteristic (0x1801/0x2A05, indicate-capable, and it enumerates BEFORE
  // the vendor services on nearly every dongle) — we then subscribed to a dead
  // characteristic, ELM replies never arrived, and init timed out forever
  // (state stuck failed/reconnecting). Skip the standard GAP/GATT/DeviceInfo
  // services and PREFER a service that contains BOTH a notify-ish char and a
  // writable char — the vendor-UART pattern every BLE ELM327 uses.
  const NimBLEUUID SVC_GAP((uint16_t)0x1800), SVC_GATT((uint16_t)0x1801), SVC_DIS((uint16_t)0x180A);
  std::vector<NimBLERemoteService*>* svcs = g_client->getServices(true);   // 1.4.x: pointer
  if (svcs) for (auto svc : *svcs) {
    if (svc->getUUID() == SVC_GAP || svc->getUUID() == SVC_GATT || svc->getUUID() == SVC_DIS)
      continue;   // standard housekeeping services — never the ELM UART
    NimBLERemoteCharacteristic* rx = nullptr;
    NimBLERemoteCharacteristic* tx = nullptr;
    std::vector<NimBLERemoteCharacteristic*>* chs = svc->getCharacteristics(true);
    if (chs) for (auto ch : *chs) {
      if (!rx && (ch->canNotify() || ch->canIndicate())) rx = ch;
      if (!tx && (ch->canWrite()  || ch->canWriteNoResponse())) tx = ch;
    }
    if (rx && tx) { g_rx = rx; g_tx = tx; break; }   // same-service pair = the UART
    if (!g_rx && rx) g_rx = rx;   // fallbacks if no single service has both
    if (!g_tx && tx) g_tx = tx;
  }
  if (!g_tx || !g_rx) { g_state = FAILED; g_client->disconnect(); blePhase(0); return false; }
  // Subscribe with the mode the characteristic actually supports — subscribing
  // for notifications (CCCD=1) on an indicate-only char silently gets nothing.
  g_rx->subscribe(g_rx->canNotify(), notifyCB);

  // NAME FALLBACK: if the scan never yielded a name, read the GAP Device Name
  // characteristic (0x1800/0x2A00) — mandatory on every BLE device, so we get
  // a real name even from dongles that never advertise one. The dash adopts
  // it into the saved pairing (see dashHealthTick).
  if (!g_conn_name[0] || strcmp(g_conn_name, "(unnamed)") == 0) {
    NimBLERemoteService* gap = g_client->getService(NimBLEUUID((uint16_t)0x1800));
    if (gap) {
      NimBLERemoteCharacteristic* nmch = gap->getCharacteristic(NimBLEUUID((uint16_t)0x2A00));
      if (nmch && nmch->canRead()) {
        std::string v = nmch->readValue();
        if (!v.empty()) {
          strncpy(g_conn_name, v.c_str(), sizeof(g_conn_name) - 1);
          g_conn_name[sizeof(g_conn_name) - 1] = 0;
        }
      }
    }
  }

  g_state = INIT;
  elmCmd("ATZ", 2000); vTaskDelay(pdMS_TO_TICKS(200));   // reset
  elmCmd("ATE0", 1000);   // echo off
  elmCmd("ATL0", 1000);   // linefeeds off
  elmCmd("ATS0", 1000);   // spaces off
  elmCmd("ATH0", 1000);   // headers off
  elmCmd("ATSP0", 1000);  // auto protocol
  // PROTOCOL LOCK: with ATSP0 the FIRST query triggers the ELM's bus-protocol
  // search, which can take 3-8 s — far beyond the normal 1.2 s poll timeout,
  // so the first coolant reads would all "time out" and the feature looks
  // dead for a while. Burn the search here ONCE with a 10 s budget (0100 =
  // supported-PIDs probe); every later poll is then fast.
  if (elmCmd("0100", 10000)) {
    char r[28]; snapResp(r, sizeof(r));
    strncpy(g_last_resp, r, sizeof(g_last_resp) - 1);
    g_last_resp[sizeof(g_last_resp) - 1] = 0;
  }
  d_last_ms = millis();
  g_state = POLL;
  blePhase(4);   // connected — stays set while linked (power-off reads POWERON = ignored)
  return true;
}

static void pollOnce() {
  static uint8_t idx = 0;
  char resp[64];
  switch (idx) {
    case 0:  // coolant  0105 -> 4105 XX : °C = XX-40
      if (elmCmd("0105", 2500)) { snapResp(resp, sizeof(resp));
        int a = pidByte(resp, "4105", 0);
        if (a >= 0) { d_coolant_f_x10 = (int16_t)lroundf(((a - 40) * 9.0f / 5.0f + 32.0f) * 10.0f); d_last_ms = millis(); }
        else {
          // Keep the raw reply for the Sensor page ("NODATA" etc).
          strncpy(g_last_resp, resp, sizeof(g_last_resp) - 1);
          g_last_resp[sizeof(g_last_resp) - 1] = 0;
        } }
      break;
    case 1:  // intake air temp 010F -> 410F XX : °C = XX-40
      if (elmCmd("010F", 1200)) { snapResp(resp, sizeof(resp));
        int a = pidByte(resp, "410F", 0);
        if (a >= 0) { d_iat_f_x10 = (int16_t)lroundf(((a - 40) * 9.0f / 5.0f + 32.0f) * 10.0f); d_last_ms = millis(); } }
      break;
    case 2:  // module voltage: ATRV -> "12.3V"
      if (elmCmd("ATRV", 1000)) { snapResp(resp, sizeof(resp));
        float v = atof(resp);
        if (v > 1.0f && v < 30.0f) { d_volt_x10 = (int16_t)lroundf(v * 10.0f); d_last_ms = millis(); } }
      break;
  }
  idx = (idx + 1) % 3;
}

static void obdTask(void*) {
  // Let the system settle a beat before bringing up the BT controller (the
  // radio/coex init alongside a live WiFi link is the delicate moment).
  vTaskDelay(pdMS_TO_TICKS(150));
  // Refuse to bring up the BT controller without headroom: it needs ~50-64 KB
  // of INTERNAL heap; starting it into a nearly-full heap crashes instead of
  // failing politely. Park the task with a visible reason instead.
  const size_t free_int = heap_caps_get_free_size(MALLOC_CAP_INTERNAL);
  if (free_int < 64 * 1024) {
    snprintf(g_last_err, sizeof(g_last_err), "BT skipped: low heap (%uKB free)",
             (unsigned)(free_int / 1024));
    blePhase(0);
    g_state = FAILED;
    for (;;) vTaskDelay(pdMS_TO_TICKS(1000));   // parked — no BLE this boot
  }
  // Heavy BLE stack init on the task's own stack (never the UI stack).
  NimBLEDevice::init("racedash");
  // LOW TX power EVERYWHERE (default/conn + adv + scan-req): the radio going
  // to full +9dBm draws a current spike that can brown out a marginal supply
  // (suspected BT crash cause). N0 = 0 dBm is plenty for an OBD dongle a few
  // inches away and much gentler on the rail.
  NimBLEDevice::setPower(ESP_PWR_LVL_N0);   // 1.4.x takes esp_power_level_t
  NimBLEDevice::setPower(ESP_PWR_LVL_N0, ESP_BLE_PWR_TYPE_ADV);
  NimBLEDevice::setPower(ESP_PWR_LVL_N0, ESP_BLE_PWR_TYPE_SCAN);
  blePhase(0);   // init survived -> phase back to inactive
  g_state = IDLE;
  for (;;) {
    if (g_req_shutdown) {
      // FULL radio release for the WiFi<->BLE time-share (see RaceDash.ino
      // netOwnerTick): running both radios crashes this 2.0.14/IDF-4.4 core
      // (decoded backtraces: abort() in coex_core_enable when BT enables with
      // WiFi up, and a ppTask/pm_set_sleep_type panic from the WiFi side).
      // WiFi may only start after the BT controller is fully DEINITED — a mere
      // disconnect leaves the controller running and coex still asserts.
      g_req_shutdown = false;
      if (g_client && g_client->isConnected()) { g_client->disconnect(); vTaskDelay(pdMS_TO_TICKS(300)); }
      NimBLEDevice::deinit(true);   // stops host+controller, frees clients
      g_client = nullptr; g_tx = g_rx = nullptr;
      d_coolant_f_x10 = d_iat_f_x10 = d_volt_x10 = -1;
      g_scan_n = 0; g_scanning = false;
      blePhase(0);
      g_state  = OFF;
      g_task   = nullptr;
      g_inited = false;   // begin() may re-create us later (radio handed back)
      vTaskDelete(nullptr);   // task ends here
    }
    if (g_req_stop) {
      g_req_stop = false;
      if (g_client && g_client->isConnected()) g_client->disconnect();
      g_tx = g_rx = nullptr;
      d_coolant_f_x10 = d_iat_f_x10 = d_volt_x10 = -1;
      g_state = OFF;
      blePhase(0);
    }
    if (g_req_scan) { g_req_scan = false; State prev = g_state; g_state = SCANNING; doScan(); if (prev == POLL) g_state = POLL; else g_state = IDLE; }
    if (g_req_connect) { g_req_connect = false; connectAndInit(); }

    switch (g_state) {
      case OFF:       vTaskDelay(pdMS_TO_TICKS(250)); break;
      case IDLE:      vTaskDelay(pdMS_TO_TICKS(150)); break;
      case POLL:
        if (g_client && g_client->isConnected()) { pollOnce(); vTaskDelay(pdMS_TO_TICKS(350)); }
        else { g_state = RECONNECT; }
        break;
      case RECONNECT:
        d_coolant_f_x10 = d_iat_f_x10 = d_volt_x10 = -1;
        vTaskDelay(pdMS_TO_TICKS(2500));
        connectAndInit();
        break;
      case FAILED:
        vTaskDelay(pdMS_TO_TICKS(3000));
        if (g_state == FAILED) g_state = RECONNECT;
        break;
      default:        vTaskDelay(pdMS_TO_TICKS(50)); break;   // SCANNING/CONNECTING/etc handled above
    }
  }
}

// ---- public API (called from the UI / setup / loop, core 1) ----------------
static void begin() {
  if (g_inited || g_blocked) return;
  g_inited = true;
  g_state  = IDLE;
  blePhase(1);   // covers init; the task moves the phase along (see blePhase)
  // Create the task FIRST and do NimBLEDevice::init() INSIDE it (see obdTask).
  // Doing the heavy BLE init here would run it on the shallow UI/tap-handler
  // stack (core 1) — that overflowed and crash-rebooted the board. On the task
  // it runs on the task's own stack, on core 0 (the BT/WiFi core), so it never
  // touches the UI loop. 16 KB stack: NimBLE init + the BT/coex bring-up is
  // stack-heavy; 8 KB was marginal (a possible crash cause).
  xTaskCreatePinnedToCore(obdTask, "obd", 16384, nullptr, 1, &g_task, 0);
}

static void setBlocked(bool b) { g_blocked = b; }
static bool blocked()          { return g_blocked; }
static void startScan()  { if (g_inited) g_req_scan = true; }
static bool scanning()   { return g_scanning; }
static uint8_t scanCount(){ return g_scan_n; }
static const ScanItem* scanItem(int i) { return (i >= 0 && i < g_scan_n) ? &g_scan[i] : nullptr; }

static void connectTo(const char* addr, uint8_t atype, const char* name) {
  strncpy(g_target_addr, addr, sizeof(g_target_addr) - 1); g_target_addr[sizeof(g_target_addr) - 1] = 0;
  g_target_atype = atype;
  strncpy(g_conn_name, name ? name : "", sizeof(g_conn_name) - 1); g_conn_name[sizeof(g_conn_name) - 1] = 0;
  if (!g_inited) begin();
  g_req_connect = true;
}
static void reconnectSaved() { if (g_target_addr[0]) { if (!g_inited) begin(); g_req_connect = true; } }
static void stop() { if (g_inited) g_req_stop = true; }
// Radio handover to WiFi: request a FULL teardown (disconnect + NimBLE deinit
// + task exit). Poll isDown() before starting WiFi. Processed after any
// in-flight blocking scan finishes (<= ~8 s) — callers keep a timeout.
static void requestShutdown() { if (g_inited) g_req_shutdown = true; }
static bool isDown()          { return !g_inited; }

static State    state()        { return g_state; }
static bool     connected()    { return g_state == POLL && (g_client && g_client->isConnected()); }
static bool     dataFresh()    { return d_last_ms != 0 && (millis() - d_last_ms) < 5000; }
static int16_t  coolantF_x10() { return d_coolant_f_x10; }
static int16_t  iatF_x10()     { return d_iat_f_x10; }
static int16_t  voltX10()      { return d_volt_x10; }
static const char* targetAddr(){ return g_target_addr; }
static const char* connName()  { return g_conn_name; }

static const char* stateStr() {
  switch (g_state) {
    case OFF: return "off";           case IDLE: return "idle";
    case SCANNING: return "scanning"; case CONNECTING: return "connecting";
    case DISCOVER: return "discover"; case INIT: return "init";
    case POLL: return "connected";    case RECONNECT: return "reconnecting";
    case FAILED: return "failed";     default: return "?";
  }
}

} // namespace obd

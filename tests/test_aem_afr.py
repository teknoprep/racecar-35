"""AEM 30-0300 conversion, parser, actual SD serializer, dash selection and cloud preservation."""
from pathlib import Path
import ast
import json
import math
import shutil
import subprocess
import tempfile
import unittest

from test_wifi_only import ROOT, DASH, TEENSY, block

HEADER = ROOT / 'crowpanel-arduino/RaceDash/aem_afr.h'
AEMNET_H = ROOT / 'src/aemnet.h'   # v0.1.177: the sample writer logs AEMnet keys too


def host_run(source):
    with tempfile.TemporaryDirectory(prefix='racecar-afr-test-') as tmp:
        p = Path(tmp); (p / 'test.cpp').write_text(source)
        subprocess.run(['g++', '-std=c++17', '-Wall', '-Werror', '-fsanitize=undefined',
                        '-fno-sanitize-recover=all', str(p / 'test.cpp'), '-o', str(p / 'test')], check=True)
        return subprocess.check_output([str(p / 'test')], text=True)


@unittest.skipUnless(shutil.which('g++'), 'host C++ compiler not installed')
class AemTests(unittest.TestCase):
    def test_real_conversion_and_strict_wire_parser(self):
        host_run(f'#include "{HEADER}"\n' + r'''
#include <cassert>
#include <climits>
#include <cmath>
int main() {
    using namespace aemafr;
    assert(fromMillivolts(500).afr_x100 == 850);
    assert(fromMillivolts(500).lambda_x10000 == 5801);
    assert(fromMillivolts(2500).afr_x100 == 1325);
    assert(fromMillivolts(2500).lambda_x10000 == 9043);
    assert(fromMillivolts(3000).afr_x100 == 1444);
    assert(fromMillivolts(3000).lambda_x10000 == 9853);
    assert(fromMillivolts(4500).afr_x100 == 1800);
    assert(fromMillivolts(4500).lambda_x10000 == 12285);
    assert(fromMillivolts(499).status == NOT_READY);
    assert(fromMillivolts(4501).status == ERROR);
    assert(fromMillivolts(0).afr_x100 == -1);
    assert(fromAdc(-1).status == ERROR && fromAdc(INT_MAX).status == ERROR);
    Reading out;
    for (int raw = 0; raw <= 4095; ++raw) {
        auto r = fromAdc(raw);
        assert(std::abs(r.mv - raw * 6600.0 / 4095) <= 0.501);
        assert(r.status == (r.mv < 500 ? NOT_READY : r.mv <= 4500 ? VALID : ERROR));
        if (r.status != VALID) assert(r.afr_x100 == -1 && r.lambda_x10000 == -1);
        char wire[80];
        snprintf(wire, sizeof(wire), "AFR,%u,%d,%d,%d", r.status, r.afr_x100, r.lambda_x10000, r.mv);
        assert(parseFrame(wire, out));
        assert(out.mv == r.mv && out.status == r.status);
    }
    assert(parseFrame("AFR,0,-1,-1,-1", out) && out.status == OFF);
    assert(parseFrame("AFR,3,-1,-1,-1", out) && out.status == ERROR);
    for (auto bad : {"", "AFR,", "AFR,1,1444,9853", "AFR,1,1444,9853,3000,extra",
                     "AFR,1,1445,9853,3000", "AFR,1,1444,9853,300a", "AFR,2,850,5801,500",
                     "AFR,1,999999999999999999999999999999,9853,3000", "AFR,9,-1,-1,5000",
                     "AFR,1,1444,9853, 3000", "AFR,0,-1,-1,0", "AFR,3,-1,-1,7000"}) {
        out = fromMillivolts(2500);
        assert(!parseFrame(bad, out));
        assert(out.mv == 2500);  // a rejected frame must not modify the live sample
    }
    struct { char buf[5]; char guard; } small;
    small.guard = 'Z';
    assert(jsonFragment(small.buf, sizeof(small.buf), fromMillivolts(3000)) > 5);
    assert(small.buf[4] == 0 && small.guard == 'Z');
}
'''.replace('#include <cmath>', '#include <cmath>\n#include <initializer_list>'))

    def test_actual_dash_selection_and_staleness(self):
        """The dash's REAL selection code (v0.1.177): the AEM analogue input owns AFR when it is the
        AFR source (no fall-back to any CAN value), and an input with Source = CANBUS reads ITS OWN
        bus - MegaSquirt (ECU line) or AEMnet (AEMNET line) - independently of every other input.
        The two lines fed in are the ones the real Teensy printed on the bench (MS3 + a UEGO gauge
        on one bus). Rewritten from the pre-0.1.171 afrIsVisible()/selectedAfrX10() version, whose
        functions moved into monItemValueX10() when AEM became an AFR Source option."""
        types = '\n'.join(block(DASH, sig) + ';' for sig in (
            'enum MonItem : uint8_t', 'enum MonSrc : uint8_t', 'enum MonCanBus : uint8_t',
            'struct MonCfg {', 'struct EngState {', 'struct EcuState {'))
        funcs = '\n'.join(block(DASH, sig) for sig in (
            'static int16_t aemAfrX10()', 'static void resolveEngRpm()',
            'static bool parseCanValues(const String& line, EcuState& st)',
            'static bool parseEcuLine(const String& line)', 'static bool parseAemnetLine(const String& line)',
            'static bool monItemValueX10(uint8_t item, int32_t* outX10)'))
        host_run(f'#include "{HEADER}"\n' + r'''
#include <cassert>
#include <cstdint>
#include <cstdlib>
#include <string>
uint32_t now = 10000;
uint32_t millis() { return now; }
struct String {                      // just the Arduino String surface the parser uses
    std::string v;
    String(const char* c) : v(c) {}
    String(std::string c) : v(std::move(c)) {}
    unsigned length() const { return (unsigned)v.size(); }
    char operator[](unsigned i) const { return v[i]; }
    String substring(unsigned a, unsigned b) const { return String(v.substr(a, b - a)); }
    long toInt() const { return atol(v.c_str()); }
};
constexpr int32_t MON_WARN_OFF = INT32_MIN;
''' + types + r'''
MonCfg mon_cfg;
EngState eng;
EcuState ecu, aemn;
struct { bool aem_afr = false; } s;
aemafr::Reading aem_reading;
bool aem_seen = false;
uint32_t aem_last_ms = 10000;
namespace obd { bool dataFresh() { return false; } int16_t coolantF_x10() { return -1; }
    int16_t voltX10() { return -1; } int16_t iatF_x10() { return -1; } int16_t tpsX10() { return -1; } }
''' + funcs + r'''
int32_t val(uint8_t item) { int32_t v = -12345; return monItemValueX10(item, &v) ? v : -12345; }
constexpr int32_t NONE = -12345;
void src(uint8_t item, uint8_t source, uint8_t bus = MON_CAN_MS3) { mon_cfg.src[item] = source; mon_cfg.can_bus[item] = bus; }
int main() {
    // exactly what the bench Teensy printed with MS3 + an AEM X-Series gauge on ONE bus
    assert(parseEcuLine("ECU,4420,2030,880,610,131,1040,142,-1"));
    assert(parseAemnetLine("AEMNET,-1,-1,-1,-1,124,-1,139,-1,8464,1"));
    // the typical car: everything from the MS3, AFR from the AEM gauge over AEMnet
    for (int i = 0; i < MON_COUNT; i++) src((uint8_t)i, MON_SRC_CAN, MON_CAN_MS3);
    src(MON_AFR, MON_SRC_CAN, MON_CAN_AEMNET);
    assert(val(MON_AFR) == 124);              // the gauge, not the MS3's 13.1
    assert(val(MON_TEMP) == 2030 && val(MON_MAP) == 880 && val(MON_VOLT) == 142);
    assert(val(MON_RPM) == 44200);            // RPM row: rpm x10, rounded to 10 rpm
    resolveEngRpm(); assert(eng.rpm == 4420);
    // the SAME input switched to MegaSquirt reads the MS3's AFR
    src(MON_AFR, MON_SRC_CAN, MON_CAN_MS3); assert(val(MON_AFR) == 131);
    // AEMnet inputs the gauge does not carry are NO DATA - never the MS3's value, never 0
    src(MON_TEMP, MON_SRC_CAN, MON_CAN_AEMNET); assert(val(MON_TEMP) == NONE);
    src(MON_RPM, MON_SRC_CAN, MON_CAN_AEMNET);  assert(val(MON_RPM) == NONE);
    resolveEngRpm(); assert(eng.rpm == 0);
    src(MON_VOLT, MON_SRC_CAN, MON_CAN_AEMNET); assert(val(MON_VOLT) == 139);   // gauge supply V
    // an AEM ECU on AEMnet: RPM / oil come from it (AEMnet carries oil; the MS3 broadcast has none)
    assert(parseAemnetLine("AEMNET,3000,1904,1000,400,147,1004,138,453,10034,1"));
    assert(val(MON_RPM) == 30000); resolveEngRpm(); assert(eng.rpm == 3000);
    src(MON_OIL, MON_SRC_CAN, MON_CAN_AEMNET); assert(val(MON_OIL) == 453);
    src(MON_OIL, MON_SRC_CAN, MON_CAN_MS3);    assert(val(MON_OIL) == NONE);
    // MS3 with no live CAN RPM (Teensy sends -1): no data, not a fake 0
    assert(parseEcuLine("ECU,-1,-1,-1,-1,-1,-1,-1,-1"));
    src(MON_RPM, MON_SRC_CAN, MON_CAN_MS3); assert(val(MON_RPM) == NONE);
    // each bus expires on its OWN clock: AEMnet silent for >2 s -> no data, MS3 unaffected
    assert(parseEcuLine("ECU,4420,2030,880,610,131,1040,142,-1"));
    now = 12001; assert(parseEcuLine("ECU,4420,2030,880,610,131,1040,142,-1"));
    src(MON_AFR, MON_SRC_CAN, MON_CAN_AEMNET); assert(val(MON_AFR) == NONE);
    src(MON_AFR, MON_SRC_CAN, MON_CAN_MS3);    assert(val(MON_AFR) == 131);
    now = 10000; parseAemnetLine("AEMNET,-1,-1,-1,-1,124,-1,139,-1,8464,1");
    // an unknown (future / corrupt) bus value reads as no data, never garbage
    src(MON_AFR, MON_SRC_CAN, 7); assert(val(MON_AFR) == NONE);
    // the AEM ANALOGUE input as the AFR source owns AFR: no frame -> no value, even with
    // BOTH CAN buses live (never a silent fall-back), then valid / stale / out of range
    src(MON_AFR, MON_SRC_DIRECT); s.aem_afr = true;
    assert(val(MON_AFR) == NONE);
    aem_seen = true; aem_reading = aemafr::fromMillivolts(3000);
    assert(val(MON_AFR) == 144);
    now = 12000; parseEcuLine("ECU,4420,2030,880,610,131,1040,142,-1"); assert(val(MON_AFR) == 144);
    now = 12001; assert(val(MON_AFR) == NONE);
    now = 10000;
    aem_reading = aemafr::fromMillivolts(499);  assert(val(MON_AFR) == NONE);
    aem_reading = aemafr::fromMillivolts(4501); assert(val(MON_AFR) == NONE);
    aem_reading = aemafr::fromMillivolts(3000);
    now = 100; aem_last_ms = UINT32_MAX - 500; assert(val(MON_AFR) == 144);   // millis rollover
    s.aem_afr = false; assert(val(MON_AFR) == NONE);
}
''')

    def test_real_session_writer_and_server_passthrough(self):
        sig = 'static void writeSessionSample('
        start = TEENSY.index(sig, TEENSY.index(sig) + 1)  # skip forward declaration
        fn = block(TEENSY[start:], sig)
        output = host_run(f'#include "{HEADER}"\n#include "{AEMNET_H}"\n' + r'''
#include <cassert>
#include <stdarg.h>
#include <string>
#include <iostream>
#include <limits>
constexpr size_t SESSION_LINE_CAP = 640;
constexpr uint32_t CAN_STALE_MS = 2000;
uint32_t millis() { return 10000; }
uint32_t micros() { return 10000000; }
uint32_t session_start_ms = 5000, session_start_unix = 1788966000;
uint32_t session_last_flush_ms = 10000, session_samples = 0, dbg_sdwr_max_us = 0;
bool session_file_open = true;
struct { bool aem_afr = true; bool vin_direct = true; } g_cfg;
int16_t vin_x10 = 138;   // v0.1.172 direct car voltage (A16 divider)
aemafr::Reading aem_reading;
// Full CAN / BLE stub: the sample writer now logs every live channel
// (map/iat/batt/afr_can/oil_can_psi), not just the dash's display set.
struct { uint32_t last_ms = 10000; int tps_x10 = 999; int map_x10 = 1012;
         int iat_f_x10 = 882; int bat_x10 = 139; int afr_x10 = 147;
         int oil_x10 = 421; } can_ecu;
aemnet::State can_aem;   // v0.1.177: no AEMnet device live -> no aem_* keys (test_aemnet covers them)
uint32_t bt_last_ms = 10000;
int bt_tps_x10 = 500, bt_spark_x10 = -35;
int bt_iat_f_x10 = 900, bt_volt_x10 = 141;
struct File {
    std::string bytes;
    int write(const uint8_t* p, size_t n) { bytes.append((const char*)p,n); return n; }
    void close() {} void sync() {}
} session_file;
struct { void printf(const char*, ...) {} } Serial;
void emitSessionStatus(bool) {}
''' + fn + r'''
void sample(float huge = 0) {
    writeSessionSample(3, 50, huge ? huge : -89.999999f, huge ? huge : -179.999999f,
        huge ? huge : 249.5f, true,
        huge ? huge : 299.9f, huge ? huge : 359.9f, 65535, 30000, 30000,
        huge ? huge : -2.0f, huge ? huge : -2.0f, huge ? huge : -2.0f,
        huge ? huge : -250.0f, huge ? huge : -250.0f, huge ? huge : -250.0f, 999);
}
int main() {
    for (int mv : {3000, 499, 4501, 0}) { aem_reading = aemafr::fromMillivolts(mv); sample(); }
    g_cfg.aem_afr = false; sample();
    g_cfg.aem_afr = true; session_start_unix = 0; aem_reading = aemafr::fromMillivolts(2500); sample();
    size_t saved = session_file.bytes.size();
    // Huge unphysical inputs must drop the sample, not underflow remaining buffer size.
    sample(std::numeric_limits<float>::max());
    assert(session_file.bytes.size() == saved);
    std::cout << session_file.bytes;
}
''')
        rows = [json.loads(line) for line in output.splitlines()]
        self.assertEqual(len(rows), 6)
        self.assertEqual(rows[0]['afr'], 14.44)
        self.assertEqual(rows[0]['lambda'], .9853)
        self.assertEqual(rows[0]['afr_source'], 'aem30-0300')
        self.assertEqual(rows[0]['spark_deg'], -3.5)
        # Every channel the system holds is logged, not just what the dash
        # displays: altitude (the review page's Altitude tile + /track3d read
        # it), MS3 CAN map/iat/batt/afr, and the RC35 bench CAN oil.
        self.assertEqual(rows[0]['alt_m'], 249.5)
        self.assertEqual(rows[0]['map_kpa'], 101.2)
        self.assertEqual(rows[0]['iat_f'], 88.2)      # CAN wins over the dongle
        self.assertEqual(rows[0]['batt_v'], 13.9)
        self.assertEqual(rows[0]['afr_can'], 14.7)
        self.assertEqual(rows[0]['oil_can_psi'], 42.1)
        # v0.1.172: the logger's own A16 car-voltage divider is its own key, never batt_v.
        self.assertEqual(rows[0]['vin_v'], 13.8)
        # afr_can/oil_can_psi are SEPARATE keys from the AEM afr/lambda and the
        # direct-transducer oil_psi: provenance must never be ambiguous, so both
        # oil channels ride the same line with their own key.
        self.assertEqual(rows[0]['oil_psi'], 3000.0)
        self.assertLess(max(map(len, output.splitlines())), 640)
        self.assertGreater(len(output.splitlines()[0]), 320)  # catches old upload-buffer truncation
        for row, status in zip(rows[1:4], ['not_ready', 'error', 'not_ready']):
            self.assertIsNone(row['afr']); self.assertIsNone(row['lambda'])
            self.assertEqual(row['afr_status'], status)
        self.assertNotIn('afr', rows[4])
        self.assertIn('t_ms', rows[5]); self.assertNotIn('t', rows[5])
        # Run the actual server /data loader without importing its web app/startup.
        tree = ast.parse((ROOT / 'server/app/main.py').read_text())
        node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == '_session_data_payload')
        env = {'json': json, 'math': math, 'pathlib': __import__('pathlib')}
        exec(compile(ast.Module(body=[node], type_ignores=[]), '<real server data loader>', 'exec'), env)
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / 'session.ndjson'; p.write_text(output)
            loaded = env['_session_data_payload'](p, 1, 0)
            self.assertEqual(loaded['samples'], rows)

    def test_opt_in_persistence_and_all_upload_capacities(self):
        self.assertIn('bool     aem_afr = false;', TEENSY)
        self.assertIn('bool     aem_afr            = false;', DASH)
        self.assertRegex(block(DASH, 'static void loadSettings()'), r'getBool\s*\("afraem"')
        self.assertRegex(block(DASH, 'static void saveSettings()'), r'putBool\s*\("afraem"')
        self.assertIn('CFG,afraem,%d', block(DASH, 'static void sendCfgToTeensy()'))
        self.assertNotIn('line[320]', TEENSY)
        self.assertIn('wtext[QGET_WIN][SESSION_LINE_CAP]', TEENSY)
        self.assertIn('pinMode(AEM_AFR_ADC_PIN, INPUT);', TEENSY)


if __name__ == '__main__':
    unittest.main()

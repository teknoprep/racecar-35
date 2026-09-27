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
        funcs = block(DASH, 'static bool afrIsVisible()') + '\n' + block(DASH, 'static int16_t selectedAfrX10()')
        host_run(f'#include "{HEADER}"\n' + '''
#include <cassert>
uint32_t now = 10000;
uint32_t millis() { return now; }
struct { bool aem_afr = false, show_afr = true; int sensor_type = 0; } s;
struct { uint32_t last_ms = 10000; int16_t afr_x10 = 147; } ecu;
aemafr::Reading aem_reading;
bool aem_seen = false;
uint32_t aem_last_ms = 10000;
''' + funcs + '''
int main() {
    assert(!afrIsVisible() && selectedAfrX10() == -1);
    s.sensor_type = 1;
    assert(afrIsVisible() && selectedAfrX10() == 147);
    s.aem_afr = true;
    assert(selectedAfrX10() == -1); // no AEM frame; must NOT fall back to live CAN
    aem_seen = true; aem_reading = aemafr::fromMillivolts(3000);
    for (int source=0; source<3; ++source) {
        s.sensor_type = source;
        assert(afrIsVisible() && selectedAfrX10() == 144);
    }
    now = 12000; assert(selectedAfrX10() == 144);
    now = 12001; assert(selectedAfrX10() == -1);
    now = 10000;
    aem_reading = aemafr::fromMillivolts(499); assert(selectedAfrX10() == -1);
    aem_reading = aemafr::fromMillivolts(4501); assert(selectedAfrX10() == -1);
    aem_reading = aemafr::fromMillivolts(3000);
    now = 100; aem_last_ms = UINT32_MAX - 500;
    assert(selectedAfrX10() == 144); // millis rollover
    s.show_afr = false; assert(!afrIsVisible() && selectedAfrX10() == 144);
}
''')

    def test_real_session_writer_and_server_passthrough(self):
        sig = 'static void writeSessionSample('
        start = TEENSY.index(sig, TEENSY.index(sig) + 1)  # skip forward declaration
        fn = block(TEENSY[start:], sig)
        output = host_run(f'#include "{HEADER}"\n' + r'''
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
struct { bool aem_afr = true; } g_cfg;
aemafr::Reading aem_reading;
struct { uint32_t last_ms = 10000; int tps_x10 = 999; } can_ecu;
uint32_t bt_last_ms = 10000;
int bt_tps_x10 = 500, bt_spark_x10 = -35;
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

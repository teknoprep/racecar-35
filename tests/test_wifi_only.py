"""WiFi-only removal regressions; no board or Arduino Python dependencies.
Run: python3 -m unittest discover -s tests -v
Full firmware compilation remains a separate integration check.
"""
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
DASH = (ROOT / 'crowpanel-arduino/RaceDash/RaceDash.ino').read_text()
TEENSY = (ROOT / 'src/main.cpp').read_text()


def block(source, signature):
    """Extract an actual C++ block, ignoring braces in comments/strings."""
    assert source.count(signature) == 1, signature
    start = source.index(signature)
    masked = re.sub(r'//[^\n]*|/\*.*?\*/|"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'',
                    lambda m: ' ' * len(m[0]), source, flags=re.S)
    brace = masked.index('{', start)
    depth = 1
    for i in range(brace + 1, len(masked)):
        depth += (masked[i] == '{') - (masked[i] == '}')
        if depth == 0:
            return source[start:i + 1]
    raise AssertionError('unclosed block: ' + signature)


class WifiOnlyTests(unittest.TestCase):
    def test_no_ethernet_driver_or_boot_work(self):
        for token in ['<Ethernet.h>', '<EthernetUdp.h>', 'Ethernet.', 'EthernetClient',
                      'EthernetUDP', 'setupEthernet', 'rawProbeW5500', 'eth_hw_present',
                      'httpPost(', 'wifiInetActive', 'g_cfg.inet', 'SPI.begin']:
            self.assertNotIn(token, TEENSY)
        self.assertNotIn('arduino-libraries/Ethernet', (ROOT / 'platformio.ini').read_text())

    def test_old_mode_cannot_disable_wifi(self):
        for token in ['internet_mode', 'ST_INET_MODE', 'INET_MODE_NAMES', 'active_ip']:
            self.assertNotIn(token, DASH)
        load = block(DASH, 'static void loadSettings()')
        self.assertNotRegex(load, r'prefs\.get\w+\s*\(\s*"inet"')
        save = block(DASH, 'static void saveSettings()')
        self.assertRegex(save, r'prefs\.putUChar\s*\(\s*"inet",\s*1\)')
        self.assertIn('Serial.println("CFG,inet,1")', DASH)
        self.assertIn('inet ignored: WiFi via dash only', TEENSY)

    def test_old_eth_status_cannot_overwrite_wifi_ip_or_credit_health(self):
        self.assertIn('if (line.startsWith("ETH,"))  return true;', DASH)
        self.assertNotIn('parseEthLine', DASH)
        self.assertNotIn('"ETH,"', block(DASH, 'static bool uartLineIsTelemetry('))
        self.assertIn('"%s  %ddBm", wifi_ip, (int)WiFi.RSSI()', DASH)

    def test_radio_timeshare_and_time_sync_preserved(self):
        tick = block(DASH, 'static void wifiTick()')
        self.assertLess(tick.index('if (net_owner != NET_WIFI)'), tick.index('WiFi.begin'))
        self.assertIn('WiFi.mode(WIFI_OFF)', tick)
        self.assertIn('configTime(', block(DASH, 'static void wifiKickNtp()'))
        self.assertIn('SETTIME,%lu', block(DASH, 'static void wifiTickNtp()'))
        self.assertIn('line.startsWith("SETTIME,")', TEENSY)
        self.assertIn('return wupForwardFile(path, nullptr, body_len, f);', TEENSY)
        self.assertIn('static void handleQGet(', TEENSY)

    def test_versions_lockstep(self):
        version = lambda s: re.search(r'^#define FIRMWARE_VERSION "([^"]+)"', s, re.M)[1]
        self.assertEqual(version(DASH), version(TEENSY))

    @unittest.skipUnless(shutil.which('g++'), 'host C++ compiler not installed')
    def test_real_wifi_row_visibility_for_every_sensor_mode(self):
        enum = block(DASH, 'enum SettingId : uint8_t') + ';'
        fn = block(DASH, 'static bool rowShouldShow(SettingId id)')
        fields = sorted(set(re.findall(r'\bs\.(\w+)', fn)))
        settings = '\n'.join(f'int {field} = 0;' for field in fields)
        host = ('#include <cstdint>\n#include <cassert>\n#define DASH_IS_ADVANCE 1\n'
                + enum + '\nstruct { ' + settings + ' } s;\nint sd_card_status = 0;\n'
                + fn + '''
int main() {
    for (int sensor = 0; sensor < 3; ++sensor) {
        s.sensor_type = sensor;
        for (int sd = 0; sd <= 4; ++sd) {
            sd_card_status = sd;
            assert(rowShouldShow(ST_WIFI_SSID));
            assert(rowShouldShow(ST_WIFI_PASS));
            assert(rowShouldShow(ST_WIFI_STATUS));
        }
    }
}
''')
        with tempfile.TemporaryDirectory(prefix='racecar-wifi-test-') as tmp:
            src = Path(tmp) / 'test.cpp'; binary = Path(tmp) / 'test'
            src.write_text(host)
            subprocess.run(['g++', '-std=c++17', '-Wall', '-Werror', str(src), '-o', str(binary)], check=True)
            subprocess.run([str(binary)], check=True)


if __name__ == '__main__':
    unittest.main()

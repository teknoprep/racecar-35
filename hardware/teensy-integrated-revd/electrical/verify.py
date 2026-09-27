#!/usr/bin/env python3
"""Independent pin-contract assertions on the REAL PCB (not the generator objects).
These checks supplement, not replace, ERC/DRC and electrical/physical review.
"""
from pathlib import Path
import json,math,subprocess,xml.etree.ElementTree as E
import pcbnew as k
ROOT=Path(__file__).resolve().parents[1];D=ROOT/'design';NAME='racecar-integrated-revd'
alias=json.loads((D/'COMPONENT-ALIASES.json').read_text())
b=k.LoadBoard(str(D/(NAME+'.kicad_pcb')));fps={f.GetReference():f for f in b.GetFootprints()}
checks=0
def check(ok,message):
 global checks
 checks+=1
 if not ok:raise AssertionError(message)
def pin(ref,p):
 vals={str(x.GetNetname()) for x in fps[alias[ref]].Pads() if str(x.GetNumber())==str(p)}
 check(len(vals)==1,f'{ref}.{p} missing/ambiguous pad')
 return next(iter(vals))
def expect(ref,mapping):
 for p,n in mapping.items():check(pin(ref,p)==n,f'Wrong net on {ref}.{p}: expected {n}')
expect('J1',{1:'VIN_RAW',2:'GND'})
expect('F1',{1:'VIN_RAW',2:'VIN_FUSED'})
expect('U2',{1:'+5V_MAIN',2:'+5V_MAIN',3:'GND',4:'GND',5:'GND',6:'GND',7:'VIN_FUSED',8:'VIN_FUSED',9:'VIN_PROTECTED',10:'VIN_PROTECTED',11:'',12:''})
expect('C1',{1:'VIN_PROTECTED',2:'GND'}) # polarized bulk MUST be after reverse protection
expect('D2',{1:'VIN_DIODE',2:'+5V_MAIN'})
expect('JP1',{1:'VIN_DIODE',2:'TEENSY_VIN'})
expect('U3',{1:'VIN_PROTECTED',2:'GND',3:'+3V3_AUX'})
expect('U_NET_PWR',{1:'VIN_PROTECTED',2:'GND',3:'+3V3_NET'})
expect('BT1',{1:'VBAT_CELL',2:'GND'})
expect('J_BAT',{1:'VBAT_CELL',2:'GND'})
expect('J_AFR',{1:'AFR_SIGNAL',2:'GND'})
expect('J_OIL',{1:'+5V_SENSOR',2:'GND',3:'OIL_SIGNAL'})
expect('J_COOL',{1:'NTC_SIGNAL',2:'GND'})
expect('J_TACH',{1:'TACH_IN',2:'GND'})
expect('J2',{1:'GND',2:'+5V_SCREEN',3:'DASH_RX_CABLE',4:'DASH_TX_CABLE'})
expect('U_GPS',{1:'',2:'+3V3_AUX',4:'GND',5:'',6:'',7:'GND',9:'',11:'RF_GNSS',20:'GPS_TX_AUX',21:'GPS_RX_AUX',22:'+3V3_AUX',23:'+3V3_AUX'})
expect('U_ANT',{1:'+3V3_AUX',2:'GND',3:'+3V3_AUX',4:'',5:'+3V3_AUX',6:'ANT_SWITCH'})
expect('U_IMU',{1:'GND',2:'GND',3:'GND',5:'+3V3_MCU',6:'GND',7:'GND',8:'+3V3_MCU',10:'GND',11:'GND',12:'+3V3_MCU',13:'IMU_SCL',14:'IMU_SDA'})
expect('U_ADC2',{1:'+3V3_MCU',2:'TPS_FILTER',3:'TPS_ADC',4:'+3V3_MCU',5:'BRK_FILTER',6:'BRK_ADC',7:'GND',12:'GND',13:'GND',14:'+3V3_MCU'})
expect('J_TPS',{1:'+5V_TPS',2:'GND',3:'TPS_SIGNAL'})
expect('J_BRAKE',{1:'+5V_BRK',2:'GND',3:'BRK_SIGNAL'})
expect('J_VID',{1:'GND',2:'VID_TX_CABLE',3:'VID_RX_CABLE',4:'GND'})
expect('U_ADC',{1:'+3V3_MCU',2:'OIL_FILTER',3:'OIL_ADC',4:'+3V3_MCU',5:'AFR_FILTER',6:'AFR_ADC',7:'GND',8:'NTC_ADC',9:'NTC_FILTER',10:'+3V3_MCU',11:'',12:'GND',13:'GND',14:'+3V3_MCU'})
expect('U_NET',{1:'GND',2:'+3V3_NET',3:'NET_EN',18:'NET_CS',19:'NET_MOSI',20:'NET_SCK',21:'NET_MISO',22:'NET_IRQ',27:'NET_BOOT',36:'NET_UART_RX',37:'NET_UART_TX',40:'GND',41:'GND'})
expect('U_SPI_RX',{1:'GND',2:'NET_MISO',3:'NET_MISO_MCU',4:'GND',5:'NET_IRQ_MCU',6:'NET_IRQ',7:'GND',8:'+3V3_MCU'})
expect('U_GTX',{1:'GND',2:'GPS_TX_MCU',3:'GND',4:'GPS_RX_AUX',5:'+3V3_AUX'})
expect('U_INPUT',{1:'TACH_OPTO',2:'GND',3:'GPS_TX_AUX',4:'GPS_RX_MCU',5:'+3V3_MCU',6:'TACH_MCU'})
expect('Q_NETRESET',{1:'NET_RESET_BASE',2:'GND',3:'NET_EN'})
expect('U4',{1:'+3V3_MCU',2:'GND',3:'DASH_TX_MCU',4:'DASH_TX_5V',5:'+3V3_MCU',6:'+5V_SCREEN'})
expect('U5',{1:'+3V3_MCU',2:'GND',3:'DASH_RX_MCU',4:'DASH_RX_5V',5:'GND',6:'+5V_SCREEN'})
# Physical Teensy outer-row pad map, independent of the generator's numbered GPIO list.
expect('U1',{2:'VID_RX_MCU',3:'VID_TX_MCU',7:'NET_IRQ_MCU',8:'NET_RESET_ASSERT',9:'GPS_RX_MCU',10:'GPS_TX_MCU',11:'TACH_MCU',12:'NET_CS_MCU',13:'NET_MOSI_MCU',14:'NET_MISO_MCU',15:'+3V3_MCU',16:'BRK_ADC',25:'TEENSY_VIN',28:'CAN_RX',29:'CAN_TX',30:'TPS_ADC',31:'AFR_ADC',32:'IMU_SCL',33:'IMU_SDA',34:'NTC_ADC',35:'OIL_ADC',36:'DASH_RX_MCU',37:'DASH_TX_MCU',38:'NET_SCK_MCU'})
for prefix in ('OIL','AFR','NTC','TPS','BRK'):
 expect('R_'+prefix+'H',{1:prefix+'_SIGNAL',2:prefix+'_CLAMP'})
 expect('R_'+prefix+'L',{1:prefix+'_CLAMP',2:'GND'})
 expect('D_'+prefix+'P',{1:prefix+'_CLAMP',2:'GND',3:'GND'})
 expect('D_'+prefix+'N',{1:prefix+'_CLAMP',2:'GND'})
 expect('R_'+prefix+'F',{1:prefix+'_CLAMP',2:prefix+'_FILTER'})
 check(str(fps[alias['R_'+prefix+'H']].GetValue())=='20k 0.1%','All five inputs require the new half-gain contract')
 check(str(fps[alias['R_'+prefix+'L']].GetValue())=='20k 0.1%','Divider ratio changed')
# Terminal access/order, including all physical screw terminals rather than a subset.
terms=['J_TACH','J_OIL','J_COOL','J_AFR','J_TPS','J_BRAKE','J1','J2']
found={str(f.GetReference()) for f in b.GetFootprints() if 'TerminalBlock_' in str(f.GetFPID().GetLibItemName())}
check(found=={alias[n] for n in terms},'Unexpected or omitted screw terminal')
last=-1
for n in terms:
 f=fps[alias[n]];check(abs(k.ToMM(f.GetPosition().x)-30)<.001,'Terminal edge misalignment')
 check(abs((f.GetOrientationDegrees()%360)-270)<.001,'Wire entry orientation changed')
 coords=sorted((int(p.GetNumber()),k.ToMM(p.GetPosition().y)) for p in f.Pads())
 check(all(a[1]<c[1] for a,c in zip(coords,coords[1:])),'Terminal pin direction reversed')
 check(coords[0][1]>last,'Terminal bank order changed');last=coords[-1][1]
check('ESP32-S3-WROOM-1'==str(fps[alias['U_NET']].GetFPID().GetLibItemName()),'Wrong WiFi antenna module')
check('J_WIFI_ANT' not in alias,'External WiFi antenna must remain removed')
check(any(z.GetIsRuleArea() for z in fps[alias['U_NET']].Zones()),'WiFi antenna keepout missing')
check(any(z.GetIsRuleArea() for z in b.Zones()),'GNSS reference-plane keepout missing')
cell=[(f.GetReference(),p.GetNumber()) for f in b.GetFootprints() for p in f.Pads() if p.GetNetname()=='VBAT_CELL']
check(set(cell)=={('BT1','1'),(alias['J_BAT'],'1')},'Unexpected load/charger on RTC cell')
# Numeric sanity across the operating domain (not an analog simulation).
for v in [0,.5,1,2.5,4.5,5]:check(v*.5<=2.5,'Expected normal ADC range')
for rs in [22,100,1000,2490,10000,100000]:
 parallel=1/(1/rs+1/40000);v=4.096*parallel/(2490+parallel)
 decoded=2490*v/(4.096-v-2490*v/40000)
 check(math.isclose(decoded,rs,rel_tol=1e-10),'NTC loading compensation incorrect')
# Fresh real schematic export, not a synthetic expected netlist.
report=D/'pin-contract-report.json';xml=D/'checked-netlist.xml'
subprocess.run(['kicad-cli','sch','export','netlist','--format','kicadxml','-o',str(xml),str(D/(NAME+'.kicad_sch'))],check=True)
actual={(f.GetReference(),p.GetNumber()):p.GetNetname() for f in b.GetFootprints() for p in f.Pads() if p.GetNetname()}
sch={(n.get('ref'),n.get('pin')):net.get('name') for net in E.parse(xml).findall('./nets/net') for n in net.findall('node') if not net.get('name').startswith('unconnected-')}
check(actual==sch,'Schematic/PCB connected-pad disagreement')
report.write_text(json.dumps({'checks_passed':checks,'connected_pads':len(actual),'independent_human_review':False,'physical_validation':False},indent=2)+'\n')
print(f'{checks} pin/geometry/calculation assertions passed; {len(actual)} schematic/PCB connected pads agree. NOT physical validation.')

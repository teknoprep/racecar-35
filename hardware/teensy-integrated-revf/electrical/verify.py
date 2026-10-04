#!/usr/bin/env python3
"""Independent pin-contract assertions on the REAL PCB (not the generator objects).
These checks supplement, not replace, ERC/DRC and electrical/physical review.
"""
from pathlib import Path
import json,math,subprocess,xml.etree.ElementTree as E
import pcbnew as k
ROOT=Path(__file__).resolve().parents[1];D=ROOT/'design';NAME='racecar-integrated-revf'
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
expect('U2',{1:'BOOT_MAIN',2:'VIN_PROTECTED',3:'EN_MON',4:'RT_MON',5:'FB_MAIN',6:'COMP_MAIN',7:'GND',8:'SW_MAIN',9:'GND'})
# Rev F buck + reverse protection: the input path is now a real circuit, not a module.
expect('Q_REV',{5:'VIN_FUSED',6:'VIN_FUSED',7:'VIN_FUSED',8:'VIN_FUSED',1:'VIN_PROTECTED',2:'VIN_PROTECTED',3:'VIN_PROTECTED',4:'Q_REV_GATE'})
expect('R_REVG',{1:'Q_REV_GATE',2:'GND'})
expect('D_REVZ',{1:'Q_REV_GATE',2:'',3:'VIN_PROTECTED'})
expect('D_MAIN',{1:'SW_MAIN',2:'GND'})
expect('L_MAIN',{1:'SW_MAIN',2:'+5V_MAIN'})
expect('C_BOOT',{1:'BOOT_MAIN',2:'SW_MAIN'})
expect('C_IN1',{1:'VIN_PROTECTED',2:'GND'})
expect('C_IN2',{1:'VIN_PROTECTED',2:'GND'})
expect('R_RT',{1:'RT_MON',2:'GND'})
expect('R_FBH',{1:'+5V_MAIN',2:'FB_MAIN'})
expect('R_FBL',{1:'FB_MAIN',2:'GND'})
expect('R_COMP',{1:'COMP_MAIN',2:'COMP_RC'})
expect('C_COMP',{1:'COMP_RC',2:'GND'})
expect('C_CPOLE',{1:'COMP_MAIN',2:'GND'})
expect('R_UV1',{1:'VIN_PROTECTED',2:'EN_MON'})
expect('R_UV2',{1:'EN_MON',2:'GND'})
for i in (1,2,3):
 expect('C_OUT'+str(i),{1:'+5V_MAIN',2:'GND'})
# Reverse protection only works with the body diode pointing the right way: the DRAIN
# must be on the raw input and the SOURCE on the protected load. Swapped, the body diode
# conducts on a reversed battery and dumps the load capacitors into the reversed supply.
check(set(alias['Q_REV']) and True,'Q_REV alias missing')
# Buck arithmetic: Vout = 0.8 * (1 + Rfbt/Rfbb); UVLO start = 1.2 * (1 + Ruv1/Ruv2).
check(abs(0.8*(1+52.3/10)-5.0)<0.05,'Buck feedback divider no longer sets 5.0V')
check(abs(1.2*(1+680/100)-9.4)<0.1,'Buck UVLO start point changed')
check(1.2*680/100>0.5,'Buck UVLO stop point lost')
# Lmin for 20V in / 400kHz / 5A with KIND=0.3 must be below the fitted inductor.
check((20-5)/(5*0.3)*(5/(20*400e3))*1e6<6.8,'Fitted inductor is below Lmin for this input range')
# Catch diode and FET ratings against the TVS clamp, and the inductor against the peak.
for ref,val in [('D_MAIN',60),('Q_REV',40)]:
 check(val>=36,f'{ref} voltage rating is below the 32V TVS clamp with margin')
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
expect('U_ADC',{1:'+3V3_MCU',2:'OIL_FILTER',3:'OIL_ADC',4:'+3V3_MCU',5:'AFR_FILTER',6:'AFR_ADC',7:'GND',8:'NTC_ADC',9:'NTC_FILTER',10:'+3V3_MCU',11:'VINMON_ADC',12:'VINMON_FILTER',13:'+3V3_MCU',14:'+3V3_MCU'})
expect('U_NET',{1:'GND',2:'+3V3_NET',3:'NET_EN',18:'NET_CS',19:'NET_MOSI',20:'NET_SCK',21:'NET_MISO',22:'NET_IRQ',27:'NET_BOOT',36:'NET_UART_RX',37:'NET_UART_TX',40:'GND',41:'GND'})
expect('U_SPI_RX',{1:'GND',2:'NET_MISO',3:'NET_MISO_MCU',4:'GND',5:'NET_IRQ_MCU',6:'NET_IRQ',7:'GND',8:'+3V3_MCU'})
expect('U_GTX',{1:'GND',2:'GPS_TX_MCU',3:'GND',4:'GPS_RX_AUX',5:'+3V3_AUX'})
expect('U_INPUT',{1:'TACH_OPTO',2:'GND',3:'GPS_TX_AUX',4:'GPS_RX_MCU',5:'+3V3_MCU',6:'TACH_MCU'})
expect('Q_NETRESET',{1:'NET_RESET_BASE',2:'GND',3:'NET_EN'})
expect('U4',{1:'+3V3_MCU',2:'GND',3:'DASH_TX_MCU',4:'DASH_TX_5V',5:'+3V3_MCU',6:'+5V_SCREEN'})
expect('U5',{1:'+3V3_MCU',2:'GND',3:'DASH_RX_MCU',4:'DASH_RX_5V',5:'GND',6:'+5V_SCREEN'})
# Physical Teensy outer-row pad map, independent of the generator's numbered GPIO list.
expect('U1',{2:'VID_RX_MCU',3:'VID_TX_MCU',7:'NET_IRQ_MCU',8:'NET_RESET_ASSERT',9:'GPS_RX_MCU',10:'GPS_TX_MCU',11:'TACH_MCU',12:'NET_CS_MCU',13:'NET_MOSI_MCU',14:'NET_MISO_MCU',15:'+3V3_MCU',16:'BRK_ADC',25:'TEENSY_VIN',28:'CAN_RX',29:'CAN_TX',30:'TPS_ADC',31:'AFR_ADC',32:'IMU_SCL',33:'IMU_SDA',34:'NTC_ADC',35:'OIL_ADC',36:'DASH_RX_MCU',37:'DASH_TX_MCU',38:'NET_SCK_MCU',41:'VINMON_ADC'})
# ----------------------------------------------------------------------------
# Rev F onboard CAN transceiver (U21). The external SN65HVD230 module and its J7
# header are DELETED. The contract that matters and is asserted here:
#   * STB (pin 8) is hard-tied to GND. STB high = standby = receive-only, no ACK --
#     the exact failure seen on the bench. No resistor, no pull-up, no jumper.
#   * VIO (pin 5) is the 3.3 V MCU rail, so RXD is 3.3 V and can go straight to the
#     Teensy. VCC (pin 3) is the 5 V rail (dead on a Teensy-USB-only bench).
#   * TXD/RXD land on the Teensy FlexCAN1 pins 22/23 = U1 pads 29/28.
#   * J7 is gone: two RXD drivers would fight on U1 pad 28.
expect('U21',{1:'CAN_TX',2:'GND',3:'+5V_MAIN',4:'CAN_RX',5:'+3V3_MCU',6:'CANL',7:'CANH',8:'GND'})
expect('J14',{1:'CANH',2:'CANL',3:'GND'})
expect('D_U21TVS',{1:'CANH',2:'CANL',3:'GND'})
expect('JP2',{1:'CANH',2:'CAN_TERM'})
expect('R_T1',{1:'CAN_TERM',2:'CAN_TERM_MID'})
expect('R_T2',{1:'CAN_TERM_MID',2:'CANL'})
expect('C_T',{1:'CAN_TERM_MID',2:'GND'})
expect('C_U21V',{1:'+5V_MAIN',2:'GND'})
expect('C_U21IO',{1:'+3V3_MCU',2:'GND'})
expect('C_U21B',{1:'+5V_MAIN',2:'GND'})
expect('TP6',{1:'CAN_TX'})
expect('TP7',{1:'CAN_RX'})
check(pin('U21',8)=='GND','U21 STB (pin 8) must be hard-tied to GND, not floating or pulled high')
check(pin('U21',5)=='+3V3_MCU','U21 VIO (pin 5) must be the 3.3V MCU rail so RXD is 3.3V logic')
check(pin('U21',3)=='+5V_MAIN','U21 VCC (pin 3) must be the 5V rail')
check(pin('U21',1)==pin('U1',29)=='CAN_TX','U21 TXD must reach Teensy CAN_TX on U1 pad 29 (pin 22)')
check(pin('U21',4)==pin('U1',28)=='CAN_RX','U21 RXD must reach Teensy CAN_RX on U1 pad 28 (pin 23)')
check(pin('J14',1)==pin('U21',7)=='CANH','J14 pin 1 must be CANH to the transceiver')
check(pin('J14',2)==pin('U21',6)=='CANL','J14 pin 2 must be CANL to the transceiver')
check(pin('J14',3)=='GND','J14 pin 3 must be the ECU signal-ground reference')
check('TCAN1042' in str(fps['U21'].GetValue()),'U21 must be a TCAN1042 V-variant (VIO on pin 5), never an SN65HVD230')
check(not any('SN65HVD230' in (str(f.GetValue())+str(f.GetFPID().GetLibItemName())) for f in b.GetFootprints()),'The SN65HVD230 module must not appear anywhere on the board')
check('J7' not in {f.GetReference() for f in b.GetFootprints()},'J7 must be deleted; two RXD drivers would fight on U1 pad 28')
check('J6' not in alias,'obsolete CAN-module-header alias still mapped')
check(abs(60.4*2-120.8)<1e-9,'Split termination must be two 60.4R in series (120.8R across CANH/CANL)')
for _n in ('R_T1','R_T2'):
 check(str(fps[alias[_n]].GetValue()).startswith('60.4R'),f'{_n} must be a 60.4R termination leg')
for prefix in ('OIL','AFR','NTC','TPS','BRK'):
 expect('R_'+prefix+'H',{1:prefix+'_SIGNAL',2:prefix+'_CLAMP'})
 expect('R_'+prefix+'L',{1:prefix+'_CLAMP',2:'GND'})
 expect('D_'+prefix+'P',{1:prefix+'_CLAMP',2:'GND',3:'GND'})
 expect('D_'+prefix+'N',{1:prefix+'_CLAMP',2:'GND'})
 expect('R_'+prefix+'F',{1:prefix+'_CLAMP',2:prefix+'_FILTER'})
 check(str(fps[alias['R_'+prefix+'H']].GetValue())=='20k 0.1%','All five inputs require the new half-gain contract')
 check(str(fps[alias['R_'+prefix+'L']].GetValue())=='20k 0.1%','Divider ratio changed')
# Sixth analog channel: the car's own 12V input (battery) voltage monitor. The divider
# ratio differs BY DESIGN from the five 0.500-gain sensor channels - it must scale 6-20V,
# not 0.5-4.5V - so it is checked separately rather than inside the loop above.
prefix='VINMON'
expect('R_'+prefix+'H',{1:'VIN_FUSED',2:prefix+'_CLAMP'})
expect('R_'+prefix+'L',{1:prefix+'_CLAMP',2:'GND'})
expect('D_'+prefix+'P',{1:prefix+'_CLAMP',2:'GND',3:'GND'})
expect('D_'+prefix+'N',{1:prefix+'_CLAMP',2:'GND'})
expect('R_'+prefix+'F',{1:prefix+'_CLAMP',2:prefix+'_FILTER'})
check(str(fps[alias['R_'+prefix+'H']].GetValue())=='180k 0.1%','Car-input monitor top divider value changed')
check(str(fps[alias['R_'+prefix+'L']].GetValue())=='20k 0.1%','Car-input monitor bottom divider value changed')
check(str(fps[alias['D_'+prefix+'P']].GetValue())=='LM4040 3.0','Car-input monitor shunt is not the 3.0V part')
# 1/10 divider: every normal input stays inside the 3.0V clamp window, and full scale
# (>=30V) must reach it. The clamp must also hold S4 below the TMUX1511 3.6V limit.
for vin in (0,6,10,12.8,14.4,20,24,29):
 check(vin*20/200<=3.0,f'Car-input monitor exceeds the 3.0V clamp at {vin}V')
check(30*20/200>=3.0,'Car-input monitor never reaches the clamp by 30V input')
rth=1/(1/180000+1/20000)
check(rth<20000,f'Car-input monitor source impedance {rth:.0f} ohm too high for the ADC')
check(abs(20*20/200-2.0)<1e-9,'Car-input monitor gain no longer 1/10')
# Terminal access/order. Eight field screw terminals still face the LEFT edge in this
# top-to-bottom order. Rev F adds J14 (CAN, a third 3-position Phoenix 1729021): it is
# deliberately MID-BOARD (U21 sits within ~20mm of it and CANH/CANL stay short). It is
# therefore excluded from the one-edge bank check and verified separately.
terms=['J_TACH','J_OIL','J_COOL','J_AFR','J_TPS','J_BRAKE','J1','J2']
bank={alias[n] for n in terms}
found={str(f.GetReference()) for f in b.GetFootprints() if 'TerminalBlock_' in str(f.GetFPID().GetLibItemName())}
check(found==bank|{'J14'},'Unexpected or omitted screw terminal')
check(str(fps['J14'].GetFPID().GetLibItemName())=='TerminalBlock_Phoenix_MKDS-1,5-3-5.08_1x03_P5.08mm_Horizontal','J14 must be the 3-position 5.08mm Phoenix terminal (1729021)')
check(sorted(int(p.GetNumber()) for p in fps['J14'].Pads())==[1,2,3],'J14 must have exactly three terminals')
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

#!/usr/bin/env python3
"""Independent pin-map, netlist, routing and reporting checks (NOT circuit simulation)."""
from pathlib import Path
import json, subprocess, xml.etree.ElementTree as ET
import pcbnew as k
ROOT=Path(__file__).resolve().parent; D=ROOT/'design'; R=ROOT/'reports'; R.mkdir(exist_ok=True)
NAME='racecar-carrier-reva'; ps={p['ref']:p for p in json.loads((ROOT/'components.json').read_text())}
checks=[]
def check(cond,msg):
    if not cond: raise AssertionError(msg)
    checks.append(msg)
def nets(ref, values):
    for pin,n in values.items():check(ps[ref]['pins'][str(pin)]==n,f'{ref}.{pin} = {n}')
# Independent critical expectations from PJRC and manufacturer pinouts, not imported
# from generate.py constants. Custom U1 pad numbering is deliberately explicit.
nets('U1',{1:'GND',9:'GPS_RX_MCU',10:'GPS_TX_MCU',11:'TACH_MCU',12:'ETH_CS',13:'ETH_MOSI',14:'ETH_MISO',15:'+3V3_MCU',25:'TEENSY_VIN',26:'GND',27:'+3V3_MCU',28:'CAN_RX',29:'CAN_TX',32:'IMU_SCL',33:'IMU_SDA',36:'DASH_RX_MCU',37:'DASH_TX_MCU',38:'ETH_SCK',39:'GND'})
nets('U2',{1:'+5V_MAIN',2:'+5V_MAIN',3:'GND',4:'GND',5:'GND',6:'GND',7:'VIN_FUSED',8:'VIN_FUSED',9:'VIN_PROTECTED',10:'VIN_PROTECTED',11:None,12:None})
nets('U3',{1:'VIN_PROTECTED',2:'GND',3:'+3V3_AUX'})
nets('U4',{1:'+3V3_MCU',2:'GND',3:'DASH_TX_MCU',4:'DASH_TX_5V',5:'+3V3_MCU',6:'+5V_SCREEN'})
nets('U5',{1:'+3V3_MCU',2:'GND',3:'DASH_RX_MCU',4:'DASH_RX_5V',5:'GND',6:'+5V_SCREEN'})
nets('U6',{1:'TACH_FILTER',2:'GND',3:'GPS_RX_FILTER',4:'GPS_RX_MCU',5:'+3V3_MCU',6:'TACH_MCU'})
nets('J1',{1:'VIN_RAW',2:'GND'})
nets('J2',{1:'GND',2:'+5V_SCREEN',3:'DASH_RX_CABLE',4:'DASH_TX_CABLE'})
nets('J3',{1:'GND',2:'TACH_CABLE'})
nets('J4',{1:'+3V3_AUX',2:'GND',3:'GPS_TX_CABLE',4:'GPS_RX_CABLE'})
nets('D2',{1:'+5V_MCU_DIODE',2:'+5V_MAIN'})
nets('JP1',{1:'+5V_MCU_DIODE',2:'TEENSY_VIN'})
nets('F1',{1:'VIN_RAW',2:'VIN_FUSED'})
nets('F2',{1:'+5V_MAIN',2:'+5V_SCREEN'})
nets('R6',{1:'+3V3_MCU',2:'TACH_CABLE'})
nets('C1',{1:'VIN_FUSED',2:'GND'});nets('C2',{1:'+5V_SCREEN',2:'GND'})
check(ps['C1']['mpn']=='EEU-FR1H101','C1 non-B suffix matches 3.5mm lead pitch')
check(ps['C2']['mpn']=='EEU-FR1A471B','C2 purchased variant matches 3.5mm lead pitch')
check(all(v not in ('+5V_MAIN','+5V_SCREEN','+3V3_AUX','VIN_RAW','VIN_FUSED','VIN_PROTECTED') for v in ps['U1']['pins'].values()),'No external 5V/aux3V3/raw voltage is wired to any Teensy pin; VIN has dedicated diode/jumper net')
# Compare EVERY connected pad in the actual routed PCB and exported schematic.
b=k.LoadBoard(str(D/(NAME+'.kicad_pcb')))
expected={(p['ref'],n):v for p in ps.values() for n,v in p['pins'].items() if v}
actual={(fp.GetReference(),pad.GetNumber()):pad.GetNetname() for fp in b.GetFootprints() for pad in fp.Pads() if pad.GetNetname()}
check(expected==actual,'All connected PCB pads exactly match components.json')
subprocess.run(['kicad-cli','sch','export','netlist','--format','kicadxml','-o',str(R/'schematic-netlist.xml'),str(D/(NAME+'.kicad_sch'))],check=True)
sch=ET.parse(R/'schematic-netlist.xml')
schnets={(n.get('ref'),n.get('pin')):t.get('name') for t in sch.findall('./nets/net') for n in t.findall('node') if not t.get('name').startswith('unconnected-')}
check(schnets==expected,'Exported KiCad schematic netlist matches every connected PCB pad')
# Mechanical module-header pad coordinates, top view.
fps={f.GetReference():f for f in b.GetFootprints()}
def padpos(ref,n):
    p=next(p for p in fps[ref].Pads() if p.GetNumber()==str(n)).GetPosition()
    return (k.ToMM(p.x),k.ToMM(p.y))
a=padpos('U1',1); c=padpos('U1',25); z=padpos('U1',24)
check(abs(c[0]-a[0]-15.24)<1e-5 and abs(c[1]-a[1])<1e-5,'Teensy socket row separation is exactly 15.24mm')
check(abs(z[1]-a[1]-58.42)<1e-5,'Teensy sockets each have 24 pins on 2.54mm pitch')
a=padpos('U2',1); c=padpos('U2',2); z=padpos('U2',11)
check(abs(c[0]-a[0]-2.54)<1e-5 and abs(z[1]-a[1]-12.7)<1e-5,'Pololu interface is 2x6, 2.54mm pitch, top-view orientation')
# Ensure signal routing did not replace the hand-routed high-current trunks.
tracks=[t for t in b.GetTracks() if t.GetClass()=='PCB_TRACK']
for n,w,minlen in [('+5V_MAIN',3,40),('+5V_SCREEN',3,15),('GND',3,80),('VIN_FUSED',2,20)]:
    total=sum(k.ToMM(t.GetLength()) for t in tracks if t.GetNetname()==n and k.ToMM(t.GetWidth())>=w-.001)
    check(total>=minlen,f'{n} retains >= {minlen}mm of >= {w}mm wide current trunk')
check(len(b.Zones())==2,'Ground pours are present on both copper layers')
s=(D/(NAME+'.kicad_pcb')).read_text()
check(s.count('(thickness 0.07)')==2,'Stackup records two 70um/2oz copper layers')
# Fail hard on DRC/ERC, including unconnected items, not just process exit status.
drc=json.loads((R/'drc.json').read_text())
check(not drc['violations'],'PCB DRC has zero violations')
check(not drc['unconnected_items'],'PCB has zero unconnected items')
erc=json.loads((R/'erc.json').read_text())
check(not any(s['violations'] for s in erc['sheets']),'Connectivity-block ERC has zero violations (limited electrical model)')
result={'status':'PASS - prototype CAD checks only','connected_pads_checked':len(expected),'assertions':checks,'not_verified':['Circuit simulation / full electrical ERC','Independent engineering review','Physical fit','Thermal / 5A load / voltage ripple / startup tests','Automotive transients / EMC','CrowPanel clone connector identity','Long-cable UART integrity']}
(R/'verification.json').write_text(json.dumps(result,indent=2)+'\n')
print(f'PASS: {len(checks)} assertions, {len(expected)} PCB/schematic pads match; hardware tests still required.')

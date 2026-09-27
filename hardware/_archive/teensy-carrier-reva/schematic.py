#!/usr/bin/env python3
"""Generate editable native KiCad hierarchical schematic from the PCB source nets.
Block symbols have manufacturer pin numbers and pin functions. Active pin electrical
rules are intentionally conservative; the netlist comparison is the primary check.
"""
import json, uuid, re
from pathlib import Path
ROOT=Path(__file__).resolve().parent; D=ROOT/'design'; NAME='racecar-carrier-reva'
parts={p['ref']:p for p in json.loads((ROOT/'components.json').read_text())}
def uid(s): return str(uuid.uuid5(uuid.NAMESPACE_URL,'racecar-carrier-reva/sch/'+s))
def q(s): return '"'+str(s).replace('\\','\\\\').replace('"','\\"').replace('\n','\\n')+'"'
def fx(size=1.0,justify='',hide=False): return f'(effects (font (size {size} {size}))'+(f' (justify {justify})' if justify else '')+(' (hide yes)' if hide else '')+')'
def txt(s,x,y,size=1.27): return f'(text {q(s)} (at {x} {y} 0) {fx(size,"left")} (uuid {uid(s+str(x)+str(y))}))'
rootid=uid('root')
left=['GND']+[f'GPIO {i}' for i in range(13)]+['3V3 OUT']+[f'GPIO {i}' for i in range(24,33)]
right=['VIN','GND','3V3 OUT']+[f'GPIO {i}' for i in range(23,12,-1)]+['GND']+[f'GPIO {i}' for i in range(41,32,-1)]
pin_names={
 'U1':{str(i+1):n for i,n in enumerate(left+right)},
 'U2':dict(zip(map(str,range(1,13)),['VOUT','VOUT','GND','GND','GND','GND','VIN','VIN','VRP','VRP','EN','PG'])),
 'U3':{'1':'VIN','2':'GND','3':'3V3 OUT'},
 'U4':{'1':'VCCA','2':'GND','3':'A (TX in)','4':'B (TX out)','5':'DIR HIGH','6':'VCCB'},
 'U5':{'1':'VCCA','2':'GND','3':'A (RX out)','4':'B (RX in)','5':'DIR LOW','6':'VCCB'},
 'U6':{'1':'1A TACH','2':'GND','3':'2A GPS RX','4':'2Y GPS RX','5':'VCC','6':'1Y TACH'},
 'J1':{'1':'+10-20V IN','2':'GND'},
 'J2':{'1':'GND','2':'+5V OUT','3':'RX from panel','4':'TX to panel'},
 'J3':{'1':'GND','2':'OPTO OUT'},
 'J4':{'1':'3V3 OUT','2':'GND','3':'to GPS RX','4':'from GPS TX'},
 'J5':{'1':'3V3 OUT','2':'GND','3':'SDA','4':'SCL'},
 'J6':{'1':'3V3 OUT','2':'GND','3':'to CTX/D','4':'from CRX/R'},
 'J7':dict(zip(map(str,range(1,9)),['3V3 OUT','GND','CS','MOSI','MISO','SCK','RESET','INT'])),
 'D2':{'1':'K','2':'A'},'D6':{'1':'K','2':'A'},
 'C1':{'1':'+','2':'-'},'C2':{'1':'+','2':'-'}
}

def geometry(p):
    pins=sorted(p['pins'],key=lambda x:int(x) if x else 0)
    if not pins: return [],5
    a=(len(pins)+1)//2
    h=max(7.62,(a+1)*2.54)
    coords=[]
    for i,n in enumerate(pins):
        side=0 if i<a else 1; j=i if side==0 else i-a
        coords.append((n,(-17.78 if side==0 else 17.78),h/2-(j+1)*2.54,0 if side==0 else 180))
    return coords,h

def symbol(p):
    ref=p['ref']; co,h=geometry(p)
    # These are connectivity blocks, not a claimed complete power/ERC model.
    # Pin function labels and verified BOM identify the real component semantics.
    fields=''.join(f'(property {q(k)} {q(v)} (at 0 {y} 0) {fx(1.27,hide=hidden)})' for k,v,y,hidden in [('Reference',re.sub(r'\d','',ref),h/2+2.54,False),('Value',p['value'],-h/2-2.54,False),('Footprint',p['footprint'],0,True),('Datasheet','',0,True)])
    ps=[]
    for n,x,y,ang in co:
        name=pin_names.get(ref,{}).get(n,n)
        ps.append(f'(pin passive line (at {x} {y} {ang}) (length 5.08) (name {q(name)} {fx(.85)}) (number {q(n)} {fx(.85)}))')
    return f'''(symbol "Carrier:{ref}" (pin_names (offset 0.508)) (in_bom yes) (on_board yes)
 {fields}
 (symbol "{ref}_0_1" (rectangle (start -12.7 {h/2}) (end 12.7 {-h/2}) (stroke (width .254) (type default)) (fill (type background))))
 (symbol "{ref}_1_1" {''.join(ps)}))'''

def label(net,x,y,side,key):
    # Wire terminates outside pin; global labels connect across child sheets.
    return f'(global_label {q(net)} (shape passive) (at {x} {y} {180 if side==0 else 0}) {fx(.85,"right" if side==0 else "left")} (uuid {uid(key)}))'

def instance(p,x,y,sheet):
    x=round(x/1.27)*1.27; y=round(y/1.27)*1.27
    ref=p['ref']; co,h=geometry(p); sid=uid(sheet)
    fields=''.join(f'(property {q(k)} {q(v)} (at {x} {yy} 0) {fx(size,hide=hidden)})' for k,v,yy,size,hidden in [('Reference',ref,y-h/2-2.54,1.27,False),('Value',p['value'],y+h/2+2.54,1.0,False),('Footprint',p['footprint'],y,.8,True),('Datasheet','',y,.8,True)])
    inst=f'''(symbol (lib_id "Carrier:{ref}") (at {x} {y} 0) (unit 1) (in_bom yes) (on_board yes) (dnp no) (uuid {uid(ref)}) {fields}
    (instances (project {q(NAME)} (path "/{rootid}/{sid}" (reference {q(ref)}) (unit 1)))))'''
    bits=[inst]
    for n,xx,yy,ang in co:
        endx=x+xx; endy=y-yy; net=p['pins'][n]
        if not net:
            bits.append(f'(no_connect (at {endx} {endy}) (uuid {uid(ref+"nc"+n)}))'); continue
        ox=endx+(-5.08 if xx<0 else 5.08)
        bits.append(f'(wire (pts (xy {endx} {endy}) (xy {ox} {endy})) (stroke (width 0) (type default)) (uuid {uid(ref+"wire"+n)}))')
        bits.append(label(net,ox,endy,0 if xx<0 else 1,ref+'label'+n))
    return '\n'.join(bits)

groups=[
 ('01-power','Power: 10-20V input / 5V / separate 3V3', ['J1','F1','D1','C1','U2','U3','C3','C4','D2','JP1','F2','C2','D6','R14','TP1','TP2','TP3','TP4']),
 ('02-mcu','Teensy 4.1 / expansion connectors', ['U1','C5','TP5','J4','J5','J6','J7','R10','R11','R12','R13','C12','C13','C14','C15']),
 ('03-screen','CrowPanel Advance J10: 5V power and translated UART',['J2','U4','U5','R1','R2','R3','R4','D3','D4','C6','C7','C8','C9']),
 ('04-inputs','External optocoupler and GPS input buffers',['J3','U6','R5','R6','D5','C10','C11','R7','R8','R9']),
]
notes={
 '01-power':['UNVALIDATED PROTOTYPE. Regulator module provides 5V; PCB total 5V budget 5A, screen branch fused 4A.',
 'SMBJ20CA + fuse is NOT ISO 7637 / ISO 16750 load-dump qualification. Add harness fuse near battery.',
 'U3 takes VIN_PROTECTED from Pololu VRP. AUX 3V3 and Teensy 3V3 are DIFFERENT rails.',
 'JP1 is initial-test isolation only. CUT the Teensy VUSB-VIN bridge before dual USB/carrier power.'],
 '02-mcu':['U1 pad numbering: LEFT 1..24 and RIGHT 25..48, top to bottom viewed from above; USB at top.',
 'External modules not integrated. J6 carries LOGIC, never attach CANH/CANL here. J7 is a custom harness.',
 'No direct oil/coolant ADC conditioning in Rev A. Optional modules must be 3.3V logic.',
 'Do not independently USB-power GPS while attached; it can backfeed its supply or UART.'],
 '03-screen':['J2 pin3 receives screen TXD0_H; J2 pin4 drives screen RXD0_H. Direction is relative to THIS PCB.',
 'Use the screen ADVANCE J10 5V input header. NEVER connect +5V to screen J2/HY2.0 3V3_OUT.',
 'U4 DIR=HIGH: 3V3 A -> 5V B. U5 DIR=LOW: 5V B -> 3V3 A. Ioff/VCC isolation on translators.',
 'UART TTL, NOT RS232/RS485. Long trunk-to-cabin cable at 921600 baud is NOT yet validated.'],
 '04-inputs':['J3 accepts external optocoupler transistor output or 0-3.3V/5V logic ONLY; common GND required.',
 'Never raw coil / primary / 12V tach. R6 pulls up to MCU 3V3; R5/C11 low-pass then Schmitt buffer.',
 'U6 channel 2 buffers GPS RX. GPS TX series resistor does not make a 5V or RS232 interface safe.',
 'Pin functions are explicit; block symbols use passive ERC pins. ERC is NOT an electrical safety sign-off.']}
roots=['(kicad_sch (version 20250114) (generator "eeschema") (uuid '+rootid+') (paper "A4") (lib_symbols)',
 txt('RACECAR-35 / TEENSY CARRIER / REV A',20,20,2),txt('UNVALIDATED PROTOTYPE - read README and BRINGUP before manufacture',20,28,1.1),
 txt('10-20V DC -> Pololu D36V50F5 -> 5V, 5A total budget; fused 4A screen branch',20,36,1.0)]
for idx,(file,title,refs) in enumerate(groups):
    sid=uid(file)
    x,y=25+(idx%2)*135,55+(idx//2)*60
    roots.append(f'''(sheet (at {x} {y}) (size 110 30) (stroke (width .254) (type default)) (fill (color 0 0 0 0)) (uuid {sid})
    (property "Sheetname" {q(title)} (at {x} {y-1.27} 0) {fx(1,"left")})
    (property "Sheetfile" {q(file+'.kicad_sch')} (at {x} {y+31.27} 0) {fx(1,"left")})
    (instances (project {q(NAME)} (path "/{rootid}" (page {q(str(idx+2))})))))''')
    b=[f'(kicad_sch (version 20250114) (generator "eeschema") (uuid {sid}) (paper "A3")',
       '(lib_symbols '+''.join(symbol(parts[r]) for r in refs)+')',txt(title,15,13,2)]
    for i,r in enumerate(refs):
        if file=='02-mcu':
            if i==0: px,py=57,90
            else: px,py=150+((i-1)%3)*85,40+((i-1)//3)*40
        else: px,py=56+(i%4)*96,48+(i//4)*41
        b.append(instance(parts[r],px,py,file))
    for j,n in enumerate(notes[file]): b.append(txt(n,15,251+j*6,.95))
    b.append(')');(D/(file+'.kicad_sch')).write_text('\n'.join(b)+'\n')
roots += [txt('Socketed Teensy; external GPS/IMU/CAN/W5500 modules. Regulator modules are hand-fitted.',20,182,1.0),
 txt('Factory order: 2 layers, 1.6mm FR4, 2oz finished copper BOTH sides; electrical test required.',20,190,1.0),')']
(D/(NAME+'.kicad_sch')).write_text('\n'.join(roots)+'\n')
(D/'Carrier.kicad_sym').write_text('(kicad_symbol_lib (version 20241209) (generator "kicad_symbol_editor")\n'+''.join(symbol(p).replace('"Carrier:'+p['ref']+'"','"'+p['ref']+'"',1) for p in parts.values() if p['pins'])+')\n')
(D/'sym-lib-table').write_text('(sym_lib_table (version 7) (lib (name "Carrier")(type "KiCad")(uri "${KIPRJMOD}/Carrier.kicad_sym")(options "")(descr "Project-local component connectivity blocks")))\n')
print('Created root schematic + four native KiCad sheets')

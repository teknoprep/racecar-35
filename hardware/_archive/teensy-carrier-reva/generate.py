#!/usr/bin/env python3
"""Reproducible KiCad 9 prototype carrier. Run with /usr/bin/python3 (pcbnew).
NOT a validated automotive product. See README.md and BRINGUP.md before fabrication.
"""
from pathlib import Path
import csv, json, uuid, shutil
import pcbnew as k

ROOT=Path(__file__).resolve().parent
OUT=ROOT/'design'; OUT.mkdir(exist_ok=True)
LIB=OUT/'Racecar.pretty'; LIB.mkdir(exist_ok=True)
NAME='racecar-carrier-reva'
B=k.BOARD(); B.SetCopperLayerCount(2)
PLUGIN=k.PCB_IO_MGR.PluginFind(k.PCB_IO_MGR.KICAD_SEXP)
MM=k.FromMM
P=lambda x,y: k.VECTOR2I(MM(x),MM(y))
NET={}; PARTS=[]; FPS={}

def uid(s): return str(uuid.uuid5(uuid.NAMESPACE_URL, 'racecar-carrier-reva/'+s))
def net(s):
    if s not in NET:
        NET[s]=k.NETINFO_ITEM(B,s); B.Add(NET[s])
    return NET[s]

def text(s,x,y,size=1,layer=k.F_SilkS):
    t=k.PCB_TEXT(B); t.SetText(s); t.SetPosition(P(x,y)); t.SetTextSize(P(max(.8,size),max(.8,size)));t.SetMirrored(layer==k.B_SilkS);t.SetTextThickness(MM(.15));t.SetLayer(layer);B.Add(t); return t

def line(a,b,layer=k.F_SilkS,width=.15,parent=None):
    ob=k.PCB_SHAPE(parent or B); ob.SetShape(k.SHAPE_T_SEGMENT);ob.SetStart(P(*a));ob.SetEnd(P(*b));ob.SetLayer(layer);ob.SetWidth(MM(width))
    (parent or B).Add(ob)

def rectangle(parent,x1,y1,x2,y2,layer):
    for a,b in [((x1,y1),(x2,y1)),((x2,y1),(x2,y2)),((x2,y2),(x1,y2)),((x1,y2),(x1,y1))]: line(a,b,layer,.12 if layer==k.F_SilkS else .05,parent)

def module_footprint(name,pins,bounds):
    fp=k.FOOTPRINT(B);fp.SetFPID(k.LIB_ID('Racecar',name));fp.SetAttributes(k.FP_THROUGH_HOLE)
    for n,x,y in pins:
        pad=k.PAD(fp);pad.SetNumber(str(n));pad.SetAttribute(k.PAD_ATTRIB_PTH);pad.SetShape(k.PAD_SHAPE_RECT if n==1 else k.PAD_SHAPE_CIRCLE);pad.SetSize(P(1.9,1.9));pad.SetDrillSize(P(1,1));pad.SetLayerSet(k.LSET.AllCuMask().AddLayer(k.F_Mask).AddLayer(k.B_Mask));pad.SetPosition(P(x,y));fp.Add(pad)
    rectangle(fp,*bounds,k.F_SilkS)
    rectangle(fp,bounds[0]-.3,bounds[1]-.3,bounds[2]+.3,bounds[3]+.3,k.F_CrtYd)
    PLUGIN.FootprintSave(str(LIB),fp)
    return fp

# TOP view, USB end at y=-2.54. Two 24-pin sockets, 0.600-inch row spacing.
# Custom pad numbers run down LEFT 1..24, then down RIGHT 25..48 (not DIP order).
TEENSY=module_footprint('Teensy41_Socket',[(i+1,0,i*2.54) for i in range(24)]+[(i+25,15.24,i*2.54) for i in range(24)],(-2.54,-3.81,17.78,60.96))
# Pololu top view: outer/inner columns, VOUT,GND,GND,VIN,VRP,EN/PG.
# Electrical interface: exact 2.54mm 2x6 grid; no module mounting holes guessed.
POWER=module_footprint('Pololu_D36V50F5',[(r*2+c+1,c*2.54,r*2.54) for r in range(6) for c in range(2)],(-1.27,-6.35,24.13,19.05))

def part(ref,value,footprint,x,y,pins,mpn='',maker='',notes='',angle=0):
    if isinstance(footprint,str):
        lib,fn=footprint.split(':'); fp=k.FootprintLoad('/usr/share/kicad/footprints/'+lib+'.pretty',fn)
        assert fp, footprint
        # Vendor stock footprint is copied into the project, making it self-contained.
        PLUGIN.FootprintSave(str(LIB),fp)
        fp.SetFPID(k.LIB_ID('Racecar',fn))
    else:
        fp=footprint.Duplicate();fn=str(fp.GetFPID().GetLibItemName())
    fp.SetReference(ref);fp.SetValue(value);fp.SetPosition(P(x,y));fp.SetOrientationDegrees(angle)
    fp.Reference().SetVisible(False);fp.Value().SetVisible(False)
    B.Add(fp);FPS[ref]=fp
    allpins={}
    for pad in fp.Pads():
        n=pad.GetNumber(); s=pins.get(n,pins.get(int(n) if n.isdigit() else n))
        if s: pad.SetNet(net(s))
        allpins[n]=s
    assert len([p for p in pins if str(p) not in allpins])==0, (ref,pins,allpins)
    d=dict(ref=ref,value=value,footprint='Racecar:'+fn,pins=allpins,mpn=mpn,maker=maker,notes=notes,x=x,y=y,angle=angle)
    PARTS.append(d)
    return fp

RFP='Resistor_SMD:R_0805_2012Metric_Pad1.20x1.40mm_HandSolder'
CFP='Capacitor_SMD:C_0805_2012Metric_Pad1.18x1.45mm_HandSolder'
SOT6='Package_TO_SOT_SMD:SOT-23-6_Handsoldering'
TFP=lambda n: f'TerminalBlock_Phoenix:TerminalBlock_Phoenix_MKDS-1,5-{n}-5.08_1x{n:02d}_P5.08mm_Horizontal'
JST='Connector_JST:JST_XH_B4B-XH-A_1x04_P2.50mm_Vertical'
FUSE='Fuse:Fuse_Littelfuse-NANO2-451_453'

def resistor(ref,val,x,y,a,b,angle=0,notes=''):
    codes={'100':'1000','1k':'1001','4.7k':'4701','10k':'1002','47k':'4702'}
    return part(ref,val,RFP,x,y,{1:a,2:b},'RC0805FR-07'+{'100':'100RL','1k':'1KL','4.7k':'4K7L','10k':'10KL','47k':'47KL'}[val],'Yageo',notes,angle)

def cap(ref,x,y,a,b='GND',value='100nF',angle=0):
    mpn='GRM21BR71H104KA01L' if value=='100nF' else 'GRM21BR71H102KA01L'
    return part(ref,value,CFP,x,y,{1:a,2:b},mpn,'Murata','50V X7R',angle)

left=['GND']+[f'P{i}' for i in range(13)]+['+3V3_MCU']+[f'P{i}' for i in range(24,33)]
right=['TEENSY_VIN','GND','+3V3_MCU']+[f'P{i}' for i in range(23,12,-1)]+['GND']+[f'P{i}' for i in range(41,32,-1)]
use={'P5':'ETH_INT','P6':'ETH_RST','P7':'GPS_RX_MCU','P8':'GPS_TX_MCU','P9':'TACH_MCU','P10':'ETH_CS','P11':'ETH_MOSI','P12':'ETH_MISO','P13':'ETH_SCK','P14':'DASH_TX_MCU','P15':'DASH_RX_MCU','P18':'IMU_SDA','P19':'IMU_SCL','P22':'CAN_TX','P23':'CAN_RX'}
teensy_pins={i+1:use.get(s,s if s in ['GND','+3V3_MCU','TEENSY_VIN'] else None) for i,s in enumerate(left+right)}
part('U1','Teensy 4.1 (socketed)',TEENSY,50,28,teensy_pins,'TEENSY41','PJRC','CUT VUSB-VIN bridge before connecting USB with carrier power. 2x24 female sockets supplied separately.')
part('U2','D36V50F5 / 5V 5A budget',POWER,68,99,{1:'+5V_MAIN',2:'+5V_MAIN',3:'GND',4:'GND',5:'GND',6:'GND',7:'VIN_FUSED',8:'VIN_FUSED',9:'VIN_PROTECTED',10:'VIN_PROTECTED',11:None,12:None},'4091 / D36V50F5','Pololu','Solder BOTH rows of header pins. Reverse protection built in. EN and PG unconnected. Support above board with insulating spacer.')
part('U3','TSR 1-2433 / AUX 3V3', 'Converter_DCDC:Converter_DCDC_TRACO_TSR-1_THT',100,76,{1:'VIN_PROTECTED',2:'GND',3:'+3V3_AUX'},'TSR 1-2433','Traco Power','Powered from reverse-protected 10-20V, NOT the marginal 5V minimum. 1A nominal; budget 500mA peripherals for Rev A.')
part('J1','10-20V INPUT',TFP(2),30,111,{1:'VIN_RAW',2:'GND'},'1729018','Phoenix Contact','MKDS 1,5/2-5,08. Add external 5A fuse close to battery.')
part('J2','CROWPANEL J10 ONLY',TFP(4),110,111,{1:'GND',2:'+5V_SCREEN',3:'DASH_RX_CABLE',4:'DASH_TX_CABLE'},'1729034','Phoenix Contact','1 GND, 2 +5V OUT, 3 FROM screen TXD0_H, 4 TO screen RXD0_H. Never J2/HY2.0 3V3_OUT on screen.')
part('J3','EXTERNAL OPTO OUTPUT',TFP(2),32,54,{1:'GND',2:'TACH_CABLE'},'1729018','Phoenix Contact','Open collector or 0-3.3/5V logic ONLY. NO raw coil, ignition or 12V tach.',angle=270)
part('J4','GPS UART 3V3',JST,99,28,{1:'+3V3_AUX',2:'GND',3:'GPS_TX_CABLE',4:'GPS_RX_CABLE'},'B4B-XH-A(LF)(SN)','JST','1 3V3 OUT, 2 GND, 3 to GPS RX, 4 from GPS TX. Not a universal GPS cable pinout.')
part('J5','MPU6050 I2C 3V3',JST,123,28,{1:'+3V3_AUX',2:'GND',3:'IMU_SDA',4:'IMU_SCL'},'B4B-XH-A(LF)(SN)','JST','1 3V3, 2 GND, 3 SDA(18), 4 SCL(19). AD0 on sensor to GND.')
part('J6','CAN MODULE LOGIC',JST,99,47,{1:'+3V3_AUX',2:'GND',3:'CAN_TX',4:'CAN_RX'},'B4B-XH-A(LF)(SN)','JST','External SN65HVD230 module: CTX/D to pin3, CRX/R to pin4. NOT CANH/CANL.')
part('J7','W5500 BREAKOUT', 'Connector_PinHeader_2.54mm:PinHeader_2x04_P2.54mm_Vertical',125,47,{1:'+3V3_AUX',2:'GND',3:'ETH_CS',4:'ETH_MOSI',5:'ETH_MISO',6:'ETH_SCK',7:'ETH_RST',8:'ETH_INT'},'TSW-104-07-G-D','Samtec','Custom cable map, NOT a standard W5500 module footprint. Only a 3.3V-powered, 3.3V-logic module.')
part('JP1','MCU POWER ENABLE','Connector_PinHeader_2.54mm:PinHeader_1x02_P2.54mm_Vertical',82,30,{1:'+5V_MCU_DIODE',2:'TEENSY_VIN'},'TSW-102-07-G-S','Samtec','Fit shunt AFTER unloaded power checks. Does NOT replace cutting Teensy VUSB/VIN bridge.')
part('D2','SS14 USB backfeed barrier','Diode_SMD:D_SMA',91,35,{1:'+5V_MCU_DIODE',2:'+5V_MAIN'},'SS14','Diodes Inc.','K(1) toward JP1; A(2) toward +5V_MAIN. Still MUST cut Teensy VUSB/VIN bridge.')
part('F1','5A INPUT FUSE',FUSE,41,101,{1:'VIN_RAW',2:'VIN_FUSED'},'0451005.MRL','Littelfuse','NANO2 451 5A fast, 125V. Does not replace harness fuse at battery.')
part('F2','4A SCREEN FUSE',FUSE,108,98,{1:'+5V_MAIN',2:'+5V_SCREEN'},'0451004.MRL','Littelfuse','4A screen branch; 5A total main output budget.',angle=270)
part('D1','SMBJ20CA INPUT TVS','Diode_SMD:D_SMB',53,100,{1:'VIN_FUSED',2:'GND'},'SMBJ20CA','Littelfuse','BIDIRECTIONAL 20V standoff / 32.4V nominal rated-current clamp. Transient help, NOT load-dump certification.',angle=90)
part('C1','100uF 50V','Capacitor_THT:CP_Radial_D8.0mm_P3.50mm',55,116,{1:'VIN_FUSED',2:'GND'},'EEU-FR1H101','Panasonic','Observe + terminal; 105C low ESR.',angle=180)
part('C2','470uF 10V','Capacitor_THT:CP_Radial_D8.0mm_P3.50mm',98,112,{1:'+5V_SCREEN',2:'GND'},'EEU-FR1A471B','Panasonic','Observe + terminal; 105C low ESR.')
cap('C3',97,69,'VIN_PROTECTED');cap('C4',108,81,'+3V3_AUX');cap('C5',72,33,'+3V3_MCU')
for ref,x,di,an,bn in [('U4',130,'+3V3_MCU','DASH_TX_MCU','DASH_TX_5V'),('U5',141,'GND','DASH_RX_MCU','DASH_RX_5V')]:
    part(ref,'SN74LVC1T45',SOT6,x,91,{1:'+3V3_MCU',2:'GND',3:an,4:bn,5:di,6:'+5V_SCREEN'},'SN74LVC1T45DBVR','Texas Instruments','Fixed DIR: U4 A->B TX; U5 B->A RX. VCC isolation + Ioff; both rails decoupled.')
cap('C6',126,87,'+3V3_MCU');cap('C7',133,87,'+5V_SCREEN');cap('C8',138,87,'+3V3_MCU');cap('C9',145,87,'+5V_SCREEN')
resistor('R1','100',129,101,'DASH_TX_5V','DASH_TX_CABLE',angle=90)
resistor('R2','100',140,101,'DASH_RX_CABLE','DASH_RX_5V',angle=90)
resistor('R3','47k',126,81,'+3V3_MCU','DASH_TX_MCU')
resistor('R4','47k',138,107,'+5V_SCREEN','DASH_RX_CABLE')
for ref,x,n in [('D3',130,'DASH_TX_CABLE'),('D4',140,'DASH_RX_CABLE')]:
    part(ref,'PESD5V0S1BA','Diode_SMD:D_SOD-323',x,117,{1:n,2:'GND'},'PESD5V0S1BA,115','Nexperia','Bidirectional signal ESD TVS. NOT protection against 12V miswiring.',angle=90)
part('U6','SN74LVC2G17',SOT6,40,67,{1:'TACH_FILTER',2:'GND',3:'GPS_RX_FILTER',4:'GPS_RX_MCU',5:'+3V3_MCU',6:'TACH_MCU'},'SN74LVC2G17DBVR','Texas Instruments','3.3V Schmitt buffers; 5.5V-tolerant inputs; Ioff. Channel 1 tach, channel 2 GPS RX.')
cap('C10',44,63,'+3V3_MCU');cap('C11',35,66,'TACH_FILTER',value='1nF',angle=90)
resistor('R5','1k',40,59,'TACH_CABLE','TACH_FILTER')
resistor('R6','4.7k',35,74,'+3V3_MCU','TACH_CABLE',angle=90)
part('D5','PESD5V0S1BA','Diode_SMD:D_SOD-323',29,67,{1:'TACH_CABLE',2:'GND'},'PESD5V0S1BA,115','Nexperia','Logic-level ESD only',angle=90)
resistor('R7','100',109,35,'GPS_RX_CABLE','GPS_RX_FILTER')
resistor('R8','100',101,35,'GPS_TX_MCU','GPS_TX_CABLE')
resistor('R9','47k',110,42,'+3V3_MCU','GPS_RX_FILTER')
resistor('R10','10k',139,57,'+3V3_MCU','ETH_CS')
resistor('R11','10k',138,64,'+3V3_AUX','ETH_RST')
# On-board pullups fitted. Account for any additional module pullups in commissioning.
resistor('R12','4.7k',125,36,'+3V3_AUX','IMU_SDA')
resistor('R13','4.7k',135,36,'+3V3_AUX','IMU_SCL')
part('D6','GREEN 5V','LED_SMD:LED_0805_2012Metric',116,82,{1:'LED_K',2:'+5V_MAIN'},'LTST-C170KGKT','Lite-On','LED cathode is pad 1')
resistor('R14','4.7k',116,77,'LED_K','GND',angle=90)
# Small local decoupling at external module headers.
cap('C12',115,25,'+3V3_AUX');cap('C13',141,28,'+3V3_AUX');cap('C14',114,48,'+3V3_AUX');cap('C15',137,47,'+3V3_AUX')
for ref,x,y,n in [('TP1',42,119,'GND'),('TP2',47,119,'VIN_FUSED'),('TP3',115,92,'+5V_MAIN'),('TP4',114,70,'+3V3_AUX'),('TP5',74,43,'+3V3_MCU')]:
    part(ref,n,'TestPoint:TestPoint_THTPad_D2.0mm_Drill1.0mm',x,y,{1:n},'test pad','PCB','Probe with meter before fitting modules.')
for i,(x,y) in enumerate([(24,24),(146,24),(24,126),(146,126)],1):
    part('H'+str(i),'M3 MOUNT','MountingHole:MountingHole_3.2mm_M3',x,y,{},'M3 nylon standoff','Hardware','Use insulating standoffs; keep wiring away from screw heads.')
# Board edge and readable connector legends. Pin-1 square pad is authoritative.
rectangle(B,20,20,150,130,k.Edge_Cuts)
text('RACECAR-35 / TEENSY CARRIER',79,123,1.4)
text('REV A  --  UNVALIDATED PROTOTYPE',82,126,1.0)
text('USB: CUT TEENSY VUSB-VIN BRIDGE',66,23,0.9)
text('JP1 MCU PWR',82,38,.9)
text('J1  +10-20V  GND',34,121,.9)
text('5A IN',41,96,.9)
text('J2 SCREEN: J10 ONLY',118,121,.9)
text('1 GND  2 +5V  3 RX  4 TX',118,124,.75)
text('RX/TX RELATIVE TO THIS PCB',120,127,.7)
text('4A SCREEN FUSE',108,92,.75)
text('J3 OPTO',31,47,.9)
text('GND',23,54,.75);text('SIG',23,59,.75)
text('NO 12V',32,79,.9)
text('U6 LOGIC ONLY',38,82,.8)
text('GPS J4: 3V3 G RX TX',105,23,.8)
text('GPS RX/TX ABOVE = MODULE PINS',108,39,.6)
text('IMU J5: 3V3 G SDA SCL',131,23,.7)
text('J6 CAN MODULE',103,54,.8)
text('3V3 G CTX CRX',104,57,.75)
text('J7 W5500',128,60,.9)
text('3V3 ONLY',128,63,.8)
text('U3 AUX 3V3',101,66,.9)
text('U2 POLOLU 4091',81,95,.85)
text('5V / 5A TOTAL',81,98,.85)
text('FIT MODULE ABOVE PCB',81,116,.7)
text('3V3_AUX != TEENSY 3V3',99,85,.8)
text('Prototype only: no load-dump / EMC validation',85,128,.7,k.B_SilkS)

# Set conservative rules. Wide power trunks are hand-routed; autorouter does signals.
ds=B.GetDesignSettings(); ds.m_TrackMinWidth=MM(.25);ds.m_MinClearance=MM(.2);ds.m_CopperEdgeClearance=MM(.5);ds.m_HoleClearance=MM(.25);ds.m_SilkClearance=MM(.15)
ds.SetCustomTrackWidth(MM(.3))

# Explicit routes for the 5A/3A supply paths, no single via in a high-current trunk.
def pos(ref,p):
    pa=next(x for x in FPS[ref].Pads() if x.GetNumber()==str(p));v=pa.GetPosition();return (k.ToMM(v.x),k.ToMM(v.y))
def route(n,points,width=3,layer=k.F_Cu):
    for a,b in zip(points,points[1:]):
        if a==b: continue
        t=k.PCB_TRACK(B);t.SetStart(P(*a));t.SetEnd(P(*b));t.SetWidth(MM(width));t.SetLayer(layer);t.SetNet(net(n));t.SetLocked(True);B.Add(t)
route('VIN_RAW',[pos('J1',1),(30,101),pos('F1',1)],2)
route('VIN_FUSED',[pos('F1',2),(46,101),(49,105),(62,105),(64,106.62),pos('U2',7),pos('U2',8)],2)
route('VIN_FUSED',[(49,105),pos('D1',1)],1.5)
route('VIN_FUSED',[(60,105),(62,108),(62,116),pos('C1',1)],1)
# Parallel VOUT pins both carry current to a common low-impedance junction.
route('+5V_MAIN',[pos('U2',2),pos('U2',1),(63,99)],2.6)
route('+5V_MAIN',[(63,99),(63,90),(108,90),pos('F2',1)],3)
route('+5V_SCREEN',[pos('F2',2),(108,105),(115.08,105),pos('J2',2)],3)
route('+5V_SCREEN',[(108,105),(98,105),pos('C2',1)],1.2)
# Module duplicates MUST be joined, including the protected input used by U3.
route('VIN_PROTECTED',[pos('U2',9),pos('U2',10)],1)
# Ground trunks on BACK supplement the plane at high-current connections.
route('GND',[pos('U2',3),pos('U2',4),pos('U2',6),pos('U2',5)],1.6,k.B_Cu)
route('GND',[pos('U2',5),(65,104.08)],2.6,k.B_Cu)
route('GND',[(65,104.08),(65,121),(95,121),(97,119),(110,119),pos('J2',1)],3,k.B_Cu)
route('GND',[pos('J1',2),(35.08,124),(62,124),(65,121)],3,k.B_Cu)

B.BuildConnectivity()
k.SaveBoard(str(OUT/(NAME+'.kicad_pcb')),B)
# Project file: 2oz external copper is a FAB requirement, not a solver assertion.
project={'meta':{'filename':NAME+'.kicad_pro','version':1},'board':{'design_settings':{'rules':{'min_clearance':.2,'min_track_width':.25,'min_via_diameter':.7,'min_through_hole_diameter':.3,'min_copper_edge_clearance':.5}}},'net_settings':{'classes':[{'name':'Default','clearance':.25,'track_width':.3,'via_diameter':.8,'via_drill':.4,'microvia_diameter':.3,'microvia_drill':.1,'diff_pair_width':.25,'diff_pair_gap':.25,'diff_pair_via_gap':.25,'bus_width':12,'line_style':0,'pcb_color':'rgba(0, 0, 0, 0.000)','schematic_color':'rgba(0, 0, 0, 0.000)','wire_width':6}], 'meta':{'version':4},'netclass_assignments':{},'netclass_patterns':[]}}
(OUT/(NAME+'.kicad_pro')).write_text(json.dumps(project,indent=2)+'\n')
(OUT/'fp-lib-table').write_text('(fp_lib_table (version 7) (lib (name "Racecar")(type "KiCad")(uri "${KIPRJMOD}/Racecar.pretty")(options "")(descr "Project-local verified stock and module footprints")))\n')
(ROOT/'components.json').write_text(json.dumps(PARTS,indent=2)+'\n')
with (ROOT/'BOM.csv').open('w') as f:
    w=csv.writer(f);w.writerow(['Reference','Quantity','Value','Manufacturer','MPN','Footprint','Assembly_notes'])
    for p in PARTS:
        w.writerow([p['ref'],1,p['value'],p['maker'],p['mpn'],p['footprint'],p['notes']])
# Extra assembly parts not represented as PCB footprints.
with (ROOT/'BOM-extra.csv').open('w') as f:
    w=csv.writer(f);w.writerow(['Item','Quantity','Requirement'])
    for r in [('Teensy sockets',2,'Samtec SSQ-124-03-G-S or equivalent 1x24, 2.54mm pitch; 15.24mm row separation'),('Teensy male headers',2,'1x24 2.54mm 0.64mm square pins, solder to Teensy'),('Regulator header',1,'Samtec TSW-106-07-G-D, 2x6 2.54mm, at least 3A/contact. Solder both rows, no low-current socket'),('JP1 shunt',1,'2.54mm 2-pin shorting shunt'),('JST housing',3,'XHP-4 plus SXH-001T-P0.6 crimp contacts, explicitly wire per pin map'),('Harness fuse',1,'Inline automotive blade fuse holder, 5A fuse, fitted close to power source'),('Screen cable',1,'18AWG or heavier power pair, short twisted TX/RX+ground arrangement; see cable drop limits'),('Standoffs',4,'M3 nylon screws and standoffs'),('Regulator support',1,'Nonconductive spacer beneath Pololu module; no metal contact with underside components'),('GPS',1,'External verified u-blox UART breakout + SMA active antenna; NEO-M9N is current firmware target'),('IMU',1,'Optional 3.3V-compatible GY-521/MPU6050, AD0 grounded'),('CAN module',1,'Optional SN65HVD230 3.3V module; termination belongs at bus ends'),('Ethernet module',1,'Optional W5500 3.3V-powered module; map custom J7 cable, no assumed module pin order')]: w.writerow(r)
print(f'Generated {len(PARTS)} components, {len(NET)} nets in {OUT}')
# Re-open so the saved project netclass settings are loaded for DSN export.
b=k.LoadBoard(str(OUT/(NAME+'.kicad_pcb')))
k.ExportSpecctraDSN(b,str(OUT/(NAME+'.dsn')))
# SWIG LoadBoard does not import the project's netclass table. Explicitly set
# equivalent router rules in the DSN (micrometres), including the via padstack.
dsn=OUT/(NAME+'.dsn'); s=dsn.read_text()
s=s.replace('(width 200)', '(width 300)').replace('(clearance 200)', '(clearance 250)').replace('(clearance 50 (type smd_smd))', '(clearance 200 (type smd_smd))')
s=s.replace('Via[0-1]_600:300_um','Via[0-1]_800:400_um')
s=s.replace('(circle F.Cu 600)', '(circle F.Cu 800)').replace('(circle B.Cu 600)', '(circle B.Cu 800)')
dsn.write_text(s)

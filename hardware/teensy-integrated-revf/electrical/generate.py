#!/usr/bin/env python3
"""Rev F electrical prototype source. Generates REAL nets/parts, not the preview.
Regeneration overwrites the electrical PCB; never run after hand-routing without
saving that work. No fabrication approval or physical-validation claim is implied.
"""
from pathlib import Path
import json, csv, uuid
import pcbnew as k
ROOT=Path(__file__).resolve().parents[1]; D=ROOT/'design'; D.mkdir(exist_ok=True)
LIB=D/'RacecarC.pretty'; LIB.mkdir(exist_ok=True)
NAME='racecar-integrated-revf'
B=k.BOARD(); B.SetCopperLayerCount(4)   # Rev F: dedicated inner ground + power plane
MM=k.FromMM; P=lambda x,y:k.VECTOR2I(MM(x),MM(y))
PLUGIN=k.PCB_IO_MGR.PluginFind(k.PCB_IO_MGR.KICAD_SEXP)
OLD=k.LoadBoard(str(ROOT.parent/'_archive/teensy-carrier-reva/design/racecar-carrier-reva.kicad_pcb'))
OLD_FP={f.GetReference():f for f in OLD.GetFootprints()}
NET={}; PARTS=[]; FPS={}
def net(s):
    if s not in NET:
        NET[s]=k.NETINFO_ITEM(B,s); B.Add(NET[s])
    return NET[s]
def part(ref,value,fp,x,y,pins,mpn,maker,group,angle=0,names=None,types=None,notes=''):
    if isinstance(fp,str):
        lib,n=fp.split(':'); f=k.FootprintLoad('/usr/share/kicad/footprints/'+lib+'.pretty',n)
        if not f: raise ValueError(fp)
    else: f=fp.Duplicate(); n=str(f.GetFPID().GetLibItemName())
    # Save pristine local footprint; every chosen package travels with the project.
    for pad in f.Pads():
        pad.SetNetCode(0)
        if ref=='U_NET' and str(pad.GetNumber())=='41' and pad.GetAttribute()==k.PAD_ATTRIB_PTH:
            pad.SetDrillSize(P(.3,.3)); pad.SetSize(P(.6,.6))
    # Rev F: KEEP the footprint's own silkscreen. Moving it to F.Fab (as Rev C-E did)
    # erased EVERY pin-1 dot, diode cathode band, electrolytic polarity mark and outline
    # on the board (0 of 193 footprints had any F.SilkS) - unusable for hand assembly.
    # Exception: the ESP32 module outline hangs over the antenna notch and past the board
    # edge, so only ITS silk is moved to F.Fab (keeps the lib/board hash in sync too).
    for item in f.GraphicalItems():
        if item.GetLayer()==k.Edge_Cuts: item.SetLayer(k.F_Fab)
        elif item.GetLayer()==k.F_SilkS and ref=='U_NET': item.SetLayer(k.F_Fab)
    PLUGIN.FootprintSave(str(LIB),f)
    f.SetFPID(k.LIB_ID('RacecarC',n)); f.SetReference(ref); f.SetValue(value)
    f.SetPosition(P(x,y)); f.SetOrientationDegrees(angle)
    f.Reference().SetVisible(False); f.Value().SetVisible(False)
    B.Add(f); FPS[ref]=f
    pins={str(a):b for a,b in pins.items()}; allpins={}
    for pad in f.Pads():
        number=str(pad.GetNumber()); val=pins.get(number)
        if val: pad.SetNet(net(val))
        allpins[number]=val
    assert set(pins)<=set(allpins),(ref,set(pins)-set(allpins))
    names={str(a):b for a,b in (names or {}).items()}; types={str(a):b for a,b in (types or {}).items()}
    PARTS.append(dict(ref=ref,value=value,footprint='RacecarC:'+n,pins=allpins,
        names={n:names.get(n,n) for n in allpins},types={n:types.get(n,'passive') for n in allpins},
        mpn=mpn,maker=maker,group=group,x=x,y=y,angle=angle,notes=notes))
    return f
RFP='Resistor_SMD:R_0805_2012Metric'; CFP='Capacitor_SMD:C_0805_2012Metric'
SOT6='Package_TO_SOT_SMD:SOT-23-6'; SOT5='Package_TO_SOT_SMD:SOT-23-5'
SOT3='Package_TO_SOT_SMD:SOT-23'; SO8='Package_SO:SOIC-8_3.9x4.9mm_P1.27mm'
TFP=lambda n:f'TerminalBlock_Phoenix:TerminalBlock_Phoenix_MKDS-1,5-{n}-5.08_1x{n:02d}_P5.08mm_Horizontal'
def r(ref,val,x,y,a,b,g,angle=0,precision=False):
    code={'0':'0R','33':'33R','100':'100R','150':'150R','680':'680R','1k':'1K','2.49k':'2K49',
          '4.7k':'4K7','10k':'10K','20k':'20K','22k':'22K','47k':'47K','100k':'100K',
          '180k':'180K','240k':'240K','680k':'680K','2.2M':'2M2',
          '52.3k':'52K3','16.9k':'16K9'}[val]
    mpn=('RT0805BRD07' if precision else 'RC0805FR-07')+code+'L'
    return part(ref,val+(' 0.1%' if precision else ' 1%'),RFP,x,y,{1:a,2:b},mpn,'Yageo',g,angle,
                notes='Thin-film 0.1%' if precision else '0805; >=0.125W')
def c(ref,val,x,y,a,b,g,angle=0):
    # BOM-REV1: three Murata values have no LCSC stock; substituted with
    # electrically identical X7R 50V parts (same 0805 footprint).
    mpn,maker={'100n':('GRM21BR71H104KA01L','Murata'),'1n':('CC0805KRX7R9BB102','Yageo'),
         '2.2n':('GRM21BR71H222KA01L','Murata'),'10n':('CL21B103KBANNNC','Samsung'),
         '1u':('GRM21BR71H105KA12L','Murata'),'2.2u':('CC0805KKX7R9BB225','Yageo'),
         '10u':('GRM21BR61E106KA73L','Murata')}[val]
    return part(ref,val,CFP,x,y,{1:a,2:b},mpn,maker,g,angle,notes='X7R 50V; 10uF is X5R 25V')
def clamp(ref,volts,x,y,a,g):
    # LM4040 SOT23: K1 / A2 / pin3 may tie to A (NOT universal TL431 pinout).
    return part(ref,'LM4040 '+volts,SOT3,x,y,{1:a,2:'GND',3:'GND'},
                'LM4040AIM3-'+volts+'/NOPB','Texas Instruments',g,names={1:'K',2:'A',3:'A/NC'})
def schottky(ref,x,y,a,kath,g):
    return part(ref,'PMEG2010ER','Diode_SMD:Nexperia_CFP3_SOD-123W',x,y,{1:kath,2:a},
                'PMEG2010ER,115','Nexperia',g,names={1:'K',2:'A'})
# 01: protected input + OUR OWN 5V buck (Rev F).
#
# The Pololu D36V50F5 module is GONE. PCBWay could not source it in two attempts (it is
# not an LCSC part and Pololu direct is rationed), and a module that cannot be bought is
# not a design. This is TI's OWN 5V/5A design example for the TPS54560B (SLVSBN0C section
# 8.2) with the input range narrowed to the car's real 10-20V (TVS-clamped at 32V), so
# every value below is TI's characterised choice rather than an invented one:
#   fsw 400kHz  -> RT/CLK 240k        FB divider 52.3k/10k  (5.0V)
#   UVLO        -> EN 680k/100k       comp 16.9k + 4.7n + 47p
#   Cout 3x47uF + 6.8uH inductor + SS56 catch diode (asynchronous)
#
# Reverse-polarity protection used to live INSIDE the Pololu module (its VRP pin). It is
# now explicit, because C1's bulk capacitor and both TRACO modules sit on the protected
# rail and a reversed battery would destroy them. Q_REV is a P-channel MOSFET with its
# DRAIN on the raw input and its SOURCE on the protected load: in that orientation the
# body diode blocks a reversed supply. (Wired the other way round the body diode would
# conduct and dump the load capacitors into the reversed input.) R_REVG holds the gate at
# GND so the FET is on by default, and D_REVZ clamps |Vgs| because the input can reach the
# 32V TVS clamp while the FET is rated +/-20V.
part('Q_REV','AO4485 P-FET 40V','Package_SO:SOIC-8_3.9x4.9mm_P1.27mm',62,94,
 {5:'VIN_FUSED',6:'VIN_FUSED',7:'VIN_FUSED',8:'VIN_FUSED',1:'VIN_PROTECTED',2:'VIN_PROTECTED',3:'VIN_PROTECTED',4:'Q_REV_GATE'},
 'AO4485','Alpha & Omega Semiconductor','power',angle=180,
 names={1:'S',2:'S',3:'S',4:'G',5:'D',6:'D',7:'D',8:'D'},types={5:'power_in',1:'power_out'},
 notes='Reverse-polarity protection: DRAIN=raw input, SOURCE=protected load so the body diode blocks a reversed supply. P-Channel, 40V, 15mOhm at Vgs=-10V. Vds 40V vs the 32V TVS clamp.')
r('R_REVG','10k',69,91.5,'Q_REV_GATE','GND','power')
part('D_REVZ','BZX84C15','Package_TO_SOT_SMD:SOT-23',68,94.5,{1:'Q_REV_GATE',3:'VIN_PROTECTED'},
 'BZX84C15','Nexperia','power',names={1:'A',2:'NC',3:'K'},
 notes='15V gate-source clamp for Q_REV: Vgs rating is +/-20V, the input can reach 32V.')
part('C_BOOT','100n','Capacitor_SMD:C_0805_2012Metric',72.5,93,{1:'BOOT_MAIN',2:'SW_MAIN'},'GRM21BR71H104KA01L','Murata','power',
 notes='Bootstrap capacitor BOOT-SW, 50V rated for a 20V rail.')
part('U2','TPS54560B 5V 5A BUCK','Package_SO:SOIC-8-1EP_3.9x4.9mm_P1.27mm_EP2.41x3.3mm',72.5,101,
 {1:'BOOT_MAIN',2:'VIN_PROTECTED',3:'EN_MON',4:'RT_MON',5:'FB_MAIN',6:'COMP_MAIN',7:'GND',8:'SW_MAIN',9:'GND'},
 'TPS54560BDDAR','Texas Instruments','power',
 names={1:'BOOT',2:'VIN',3:'EN',4:'RT/CLK',5:'FB',6:'COMP',7:'GND',8:'SW',9:'PowerPAD'},
 types={2:'power_in',8:'power_out',9:'power_in'},
 notes='60V 5A asynchronous buck at 400kHz, TI 5V/5A design example. PowerPAD (pad 9) is GND and carries the heat: thermal vias north and south of the pad, NOT inside it, so no via-in-pad resin fill is needed.')
for vx,vy in [(70.2,97.0),(71.4,97.0),(72.6,97.0),(73.8,97.0),(76.0,97.0),(81.5,101.5),(81.5,103.0),(79.0,103.6)]:
 v=k.PCB_VIA(B);v.SetPosition(P(vx,vy));v.SetWidth(k.FromMM(.6));v.SetDrill(k.FromMM(.3))
 v.SetViaType(k.VIATYPE_THROUGH);v.SetLayerPair(k.F_Cu,k.B_Cu);v.SetNet(net('GND'));v.SetLocked(True);B.Add(v)
for ref,x in [('C_IN1',68.5),('C_IN2',73.5)]:
 part(ref,'10uF 50V X7R 1210','Capacitor_SMD:C_1210_3225Metric',x,105.8,{1:'VIN_PROTECTED',2:'GND'},
      'GRM32ER71H106KA12L','Murata','power',notes='50V rating on a 10-20V rail; a 5A buck input needs ceramic close to the VIN pin, not only C1 bulk.')
part('D_MAIN','SS56 catch diode','Diode_SMD:D_SMB',79,99,{1:'SW_MAIN',2:'GND'},'SS56','BORN','power',angle=270,
 names={1:'K',2:'A'},notes='Asynchronous buck catch diode: 5A, 60V vs the 32V clamp. Carries the full inductor current during off-time. MUST be the SMB (DO-214AA) part - the bare SS56 is normally SMA (DO-214AC) and will not fit this D_SMB land pattern; LCSC C2687867 (BORN SS56 SMB) or Diotec SK56.')
part('L_MAIN','6.8uH 8A RMS SHIELDED','Inductor_SMD:L_Vishay_IHLP-4040',88,94.5,{1:'SW_MAIN',2:'+5V_MAIN'},
 'IHLP4040DZER6R8M01','Vishay','power',names={1:'1',2:'2'},types={2:'power_out'},
 notes='6.8uH, 23.3mOhm, 13.5A saturation / 8A RMS vs 5A DC and 5.7A peak. Lmin for 20V in / 400kHz / 5A is 6.25uH.')
for ref,x in [('C_OUT1',80),('C_OUT2',85),('C_OUT3',90)]:
 part(ref,'47uF 16V X5R 1210','Capacitor_SMD:C_1210_3225Metric',x,113.5,{1:'+5V_MAIN',2:'GND'},
      'GRM32ER61C476KE15L','Murata','power',angle=270,notes='TI design example output capacitance (3x47uF); the compensation below assumes it.')
r('R_FBH','52.3k',87,105,'+5V_MAIN','FB_MAIN','power',angle=270,precision=True)
r('R_FBL','10k',83,105,'FB_MAIN','GND','power',angle=270)
r('R_COMP','16.9k',78.5,105.8,'COMP_MAIN','COMP_RC','power')
part('C_COMP','4.7n','Capacitor_SMD:C_0805_2012Metric',82,109.5,{1:'COMP_RC',2:'GND'},'CL21B472KBANNNC','Samsung','power',
 notes='Compensation zero at the modulator pole (TI 8.2.2.11).')
part('C_CPOLE','47p','Capacitor_SMD:C_0805_2012Metric',86.5,109.5,{1:'COMP_MAIN',2:'GND'},'CL21C470JBANNNC','Samsung','power',
 notes='High-frequency compensation pole.')
r('R_RT','240k',68.5,109.5,'RT_MON','GND','power')
r('R_UV1','680k',73,109.5,'VIN_PROTECTED','EN_MON','power')
r('R_UV2','100k',77.5,109.5,'EN_MON','GND','power')

for ref,x,y,out in [('U3',106,74,'+3V3_AUX'),('U_NET_PWR',120,111,'+3V3_NET')]:
 part(ref,'TSR 1-2433','Converter_DCDC:Converter_DCDC_TRACO_TSR-1_THT',x,y,{1:'VIN_PROTECTED',2:'GND',3:out},
      'TSR 1-2433','TRACO Power','power',names={1:'VIN',2:'GND',3:'VOUT'},types={1:'power_in',3:'power_out'},
      notes='Separate 1A module fed by the protected rail (10-20V), NOT the marginal 5V input.')
 c('C_'+ref+'I','100n',x-4,y-7,'VIN_PROTECTED','GND','power')
 c('C_'+ref+'O','10u',x+9,y+5,out,'GND','power')
part('J1','POWER IN',TFP(2),30,131,{1:'VIN_RAW',2:'GND'},'1729018','Phoenix Contact','power',270,
     names={1:'10-20V IN',2:'GND'},types={1:'power_out',2:'power_out'})
for ref,x,y,val,mpn,a,b,ang in [('F1',40,112,5,'0451005.MRL','VIN_RAW','VIN_FUSED',0),('F2',46,132,3,'0451003.MRL','+5V_MAIN','+5V_SCREEN',180)]:
 part(ref,str(val)+'A fuse','Fuse:Fuse_Littelfuse-NANO2-451_453',x,y,{1:a,2:b},mpn,'Littelfuse','power',ang)
part('D1','SMBJ20CA','Diode_SMD:D_SMB',46,107,{1:'VIN_FUSED',2:'GND'},'SMBJ20CA','Littelfuse','power',notes='Bidirectional TVS; NOT automotive load-dump qualification')
part('C1','100uF 50V','Capacitor_THT:CP_Radial_D8.0mm_P3.50mm',55,116,{1:'VIN_PROTECTED',2:'GND'},'EEU-FR1H101','Panasonic','power',180)
part('C2','470uF 10V','Capacitor_THT:CP_Radial_D8.0mm_P3.50mm',103,119,{1:'+5V_SCREEN',2:'GND'},'EEU-FR1A471B','Panasonic','power')
part('D2','SS14','Diode_SMD:D_SMA',92,29,{1:'VIN_DIODE',2:'+5V_MAIN'},'SS14','onsemi','power',names={1:'K',2:'A'},notes='BOM-REV1: onsemi is the orderable MPN (bare SS14 from Diodes Inc is not).')
part('JP1','MCU POWER','Connector_PinHeader_2.54mm:PinHeader_1x02_P2.54mm_Vertical',84,29,{1:'VIN_DIODE',2:'TEENSY_VIN'},'TSW-102-07-G-S','Samtec','power',notes='Fit shunt after unloaded rail tests; cut Teensy VUSB-VIN before dual-power use')

# 02: socketed Teensy. Pad numbering matches the proven outer-row footprint.
left=['GND']+[f'P{i}' for i in range(13)]+['+3V3_MCU']+[f'P{i}' for i in range(24,33)]
right=['TEENSY_VIN','GND','+3V3_MCU']+[f'P{i}' for i in range(23,12,-1)]+['GND']+[f'P{i}' for i in range(41,32,-1)]
use={'P0':'VID_RX_MCU','P1':'VID_TX_MCU','P5':'NET_IRQ_MCU','P6':'NET_RESET_ASSERT',
 'P7':'GPS_RX_MCU','P8':'GPS_TX_MCU','P9':'TACH_MCU',
 'P10':'NET_CS_MCU','P11':'NET_MOSI_MCU','P12':'NET_MISO_MCU','P13':'NET_SCK_MCU',
 'P14':'DASH_TX_MCU','P15':'DASH_RX_MCU','P16':'OIL_ADC','P17':'NTC_ADC','P18':'IMU_SDA','P19':'IMU_SCL',
 'P20':'AFR_ADC','P21':'TPS_ADC','P22':'CAN_TX','P23':'CAN_RX','P24':'BRK_ADC',
 # Rev F: sixth analog channel - car input (battery) voltage monitor. P40 = Teensy
 # pin 40 = A16, which analog.c maps to ADC1 channel 9 (A14/A15 are ADC2-only).
 'P40':'VINMON_ADC'}
pins={i+1:use.get(n,n if n in ['GND','+3V3_MCU','TEENSY_VIN'] else None) for i,n in enumerate(left+right)}
part('U1','Teensy 4.1 SOCKETED',OLD_FP['U1'],60,28,pins,'TEENSY41','PJRC','mcu',
     names={i+1:n for i,n in enumerate(left+right)},types={15:'power_out',25:'power_in'},
     notes='CUT VUSB-VIN. Two 24-pin sockets. VBAT via separate removable 2-wire service lead to labelled Teensy VBAT/GND pads.')
c('C_MCU','100n',82,42,'+3V3_MCU','GND','mcu')
part('BT1','CR2032 HOLDER','Battery:BatteryHolder_Keystone_1058_1x2032',117,144,{1:'VBAT_CELL',2:'GND'},'1058','Keystone','mcu',
     names={1:'CELL+',2:'CELL-'},types={1:'power_out'},notes='Fit Panasonic CR2032 AFTER reflow/cleaning. Primary cell: NO charging.')
part('J_BAT','TO TEENSY VBAT/GND','Connector_JST:JST_XH_B2B-XH-A_1x02_P2.50mm_Vertical',92,137,{1:'VBAT_CELL',2:'GND'},
     'B2B-XH-A(LF)(SN)','JST','mcu',notes='Short removable lead: 1 to Teensy VBAT auxiliary pad, 2 GND. NEVER 3V3/ONOFF. Required assembly operation.')
# 02b: ONBOARD CAN TRANSCEIVER (Rev F). The external SN65HVD230 module on the old J7
# header is GONE. Measured on the bench (2026-10-03): that module class received fine
# but never DROVE the bus -- with TXD held at 0 V, CANH-CANL stayed at 0 V. With no ACK
# from the logger the ECU retransmitted one frame forever (telemetry 1.8 Hz instead of
# 100) and the second id never got through. Identical with two Teensys, two modules and
# a bare FlexCAN_T4 sketch, so the fix is to stop relying on a plug-in module: the
# transceiver is SOLDERED DOWN and its STB pin is HARD-TIED TO GND. STB high = standby =
# receive-only with no ACK -- exactly the failure we are designing out -- so there is no
# resistor, no pull-up and no jumper on that pin.
#   U21 TCAN1042HGV-Q1 / TCAN1042HGVDRQ1 (SOIC-8, AEC-Q100, +/-70 V bus fault):
#   1 TXD   2 GND   3 VCC(+5V)   4 RXD   5 VIO(+3V3)   6 CANL   7 CANH   8 STB(=GND)
# The VIO pin makes RXD a 3.3 V output, so CAN_RX needs no level shifting. VCC is the
# 5 V rail, so CAN needs J1 power and is dead on a Teensy-USB-only bench.
part('U21','TCAN1042HGV-Q1','Package_SO:SOIC-8_3.9x4.9mm_P1.27mm',131,61,
 {1:'CAN_TX',2:'GND',3:'+5V_MAIN',4:'CAN_RX',5:'+3V3_MCU',6:'CANL',7:'CANH',8:'GND'},
 'TCAN1042HGVDRQ1','Texas Instruments','can',
 names={1:'TXD',2:'GND',3:'VCC',4:'RXD',5:'VIO',6:'CANL',7:'CANH',8:'STB'},
 types={1:'input',2:'power_in',3:'power_in',4:'output',5:'power_in',6:'bidirectional',7:'bidirectional',8:'input'},
 notes='AEC-Q100 CAN transceiver, +/-70V bus fault protection, VIO=3.3V so RXD is 3.3V logic. STB (pin 8) is tied DIRECTLY to GND with no resistor: standby is receive-only (no ACK), the exact failure mode of the removed SN65HVD230 module. Acceptable alternates: TCAN1042VDRQ1, then NXP TJA1051T/3/1J (its pin 8 = S must also go to GND). Do NOT fit an SN65HVD230 (only -4/+16V bus fault) or any non-VIO TJA1051T/TJA1050 (5V RXD would damage the Teensy).')
c('C_U21V','100n',127,55.5,'+5V_MAIN','GND','can')
c('C_U21IO','100n',131,55.5,'+3V3_MCU','GND','can')
c('C_U21B','1u',135,55.5,'+5V_MAIN','GND','can')
# J14 'CAN': Phoenix 1729021, 3-position 5.08mm, the same family as J4/J11/J12.
# 1 = CANH, 2 = CANL, 3 = signal-ground reference to the ECU. The left edge cannot take
# a ninth screw terminal (see README): fitting one would need ~1mm body gaps AND no room
# would remain beside it for the transceiver, termination and TVS. So the CAN field
# terminal sits mid-board at the old J7 position, which keeps U21 within ~20mm of it and
# CANH/CANL short, as the layout rules require.
part('J14','CAN','TerminalBlock_Phoenix:TerminalBlock_Phoenix_MKDS-1,5-3-5.08_1x03_P5.08mm_Horizontal',110,61,
 {1:'CANH',2:'CANL',3:'GND'},'1729021','Phoenix Contact','can',
 names={1:'CANH',2:'CANL',3:'GND'},
 notes='1 CANH, 2 CANL, 3 GND (signal-ground reference to the ECU). Fit the split-termination jumper JP2 ONLY when this board is an end of the CAN bus.')
# ESD / transient: onsemi NUP2105L dual bidirectional CAN TVS, right at the connector.
part('D_U21TVS','NUP2105L','Package_TO_SOT_SMD:SOT-23',140,62.5,{1:'CANH',2:'CANL',3:'GND'},
 'NUP2105LT1G','onsemi','can',names={1:'LINE1',2:'LINE2',3:'GND'},
 notes='Dual bidirectional CAN bus protector at J14 (SOT-23). Alternates: Nexperia PESD2CAN,215 or TI ESD2CAN24DBZRQ1.')
# SWITCHABLE SPLIT TERMINATION: two 60.4R 1% in series = 120.8R across CANH/CANL, with
# the midpoint bypassed to GND by 4.7nF (common-mode). JP2 sits in series with the pair.
part('JP2','CAN TERM','Connector_PinHeader_2.54mm:PinHeader_1x02_P2.54mm_Vertical',140.5,55.5,{1:'CANH',2:'CAN_TERM'},
 'TSW-102-07-G-S','Samtec','can',notes='CAN TERM jumper (Samtec TSW-102 + shunt, as JP1). FIT ONLY IF THIS BOARD IS A BUS END; remove the shunt on a mid-bus tap. Do not fit while the ECU end is already terminated at 120R.')
part('R_T1','60.4R 1%','Resistor_SMD:R_0805_2012Metric',132,67,{1:'CAN_TERM',2:'CAN_TERM_MID'},
 'RC0805FR-0760R4L','Yageo','can',notes='Split-termination upper leg. Two 60.4R in series = 120.8R across CANH/CANL.')
part('R_T2','60.4R 1%','Resistor_SMD:R_0805_2012Metric',137,67,{1:'CAN_TERM_MID',2:'CANL'},
 'RC0805FR-0760R4L','Yageo','can',notes='Split-termination lower leg.')
part('C_T','4.7n','Capacitor_SMD:C_0805_2012Metric',142,67,{1:'CAN_TERM_MID',2:'GND'},
 'CL21B472KBANNNC','Samsung','can',notes='Split-termination midpoint bypass to GND (50V X7R).')
# CAN_TX / CAN_RX test points for probing the logic side after assembly.
part('TP6','CAN_TX','TestPoint:TestPoint_THTPad_D2.0mm_Drill1.0mm',118,70,{1:'CAN_TX'},'PCB test pad','PCB','mechanical')
part('TP7','CAN_RX','TestPoint:TestPoint_THTPad_D2.0mm_Drill1.0mm',122,70,{1:'CAN_RX'},'PCB test pad','PCB','mechanical')

# 03b: Raspberry Pi 5 video interconnect on Serial1 (Teensy pin 1 TX / pin 0 RX).
# 3.3V logic both ends. Deliberately separate from the 5V screen UART.
part('J_VID','PI 5 VIDEO 3V3 UART','Connector_JST:JST_XH_B4B-XH-A_1x04_P2.50mm_Vertical',158,128,
 {1:'GND',2:'VID_TX_CABLE',3:'VID_RX_CABLE',4:'GND'},'B4B-XH-A(LF)(SN)','JST','video',
 names={1:'GND',2:'TX to Pi',3:'RX from Pi',4:'GND'},types={2:'output',3:'input'},
 notes='Pi 5 UART 115200 8N1, 3.3V only. NO 5V/12V here. Cameras and USB storage stay on the Pi and are NOT powered by this board.')
r('R_VIDTX','1k',158,108,'VID_TX_MCU','VID_TX_CABLE','video')
r('R_VIDRX','1k',158,118,'VID_RX_CABLE','VID_RX_MCU','video')
for ref,x,y,n in [('D_VIDTX',152,108,'VID_TX_CABLE'),('D_VIDRX',152,118,'VID_RX_CABLE')]:
 part(ref,'UART ESD','Diode_SMD:D_SOD-323',x,y,{1:n,2:'GND'},'PESD5V0S1BA,115','Nexperia','video')

# 03: onboard ICM-42670-P. I2C address 0x68 (AP_AD0 tied to GND), AP_CS tied high
# to select the I2C interface. Polling only: INT1/INT2 intentionally unconnected,
# so no Teensy interrupt GPIO is allocated.
ICM_FP=k.FootprintLoad(str(LIB),'InvenSense_LGA-14_2.5x3.0mm_P0.50mm')
assert ICM_FP,'Missing derived ICM-42670-P land pattern; run make_icm_footprint.py first'
part('U_IMU','ICM-42670-P',ICM_FP,96,45,
 {1:'GND',2:'GND',3:'GND',5:'+3V3_MCU',6:'GND',7:'GND',8:'+3V3_MCU',10:'GND',11:'GND',12:'+3V3_MCU',13:'IMU_SCL',14:'IMU_SDA'},
 'ICM-42670-P','TDK InvenSense','imu',
 names={1:'AP_SDO/AP_AD0',2:'RESV',3:'RESV',4:'INT1',5:'VDDIO',6:'GND',7:'FSYNC',8:'VDD',9:'INT2',10:'RESV',11:'RESV',12:'AP_CS',13:'AP_SCL',14:'AP_SDA'},
 types={5:'power_in',8:'power_in',13:'input',14:'bidirectional'},
 notes='Address 0x68 (AP_AD0=GND); AP_CS=VDDIO selects I2C. Polling only; INT1/INT2 unconnected. RESV pads grounded per DS-000451.')
# DS-000451 Table 10 decoupling: VDD 100nF + 2.2uF, VDDIO 10nF. Pull-ups per datasheet.
c('C_IMUV','100n',94,40,'+3V3_MCU','GND','imu')
c('C_IMUL','10n',91,45,'+3V3_MCU','GND','imu',90)
c('C_IMUR','2.2u',96,50,'+3V3_MCU','GND','imu')
r('R_SDA','10k',101,39,'+3V3_MCU','IMU_SDA','imu')
r('R_SCL','10k',107,43,'+3V3_MCU','IMU_SCL','imu',90)

# 04: NEO-M9N, UART, separate current-limited active GPS antenna bias.
part('U_GPS','NEO-M9N-00B','RF_GPS:ublox_NEO',125,45,
 {2:'+3V3_AUX',4:'GND',7:'GND',10:'GND',11:'RF_GNSS',12:'GND',13:'GND',20:'GPS_TX_AUX',21:'GPS_RX_AUX',22:'+3V3_AUX',23:'+3V3_AUX',24:'GND'},
 'NEO-M9N-00B','u-blox','gps',names={1:'SAFEBOOT_N',2:'D_SEL',3:'TIMEPULSE',4:'EXTINT',5:'USB_DM',6:'USB_DP',7:'V_USB',8:'RESET_N',9:'VCC_RF',10:'GND',11:'RF_IN',12:'GND',13:'GND',14:'LNA_EN',15:'RESERVED',16:'RESERVED',17:'RESERVED',18:'SDA',19:'SCL',20:'TXD',21:'RXD',22:'V_BCKP',23:'VCC',24:'GND'},
 types={20:'output',21:'input',22:'power_in',23:'power_in'},notes='USB disabled: V_USB GND, D+/- open. No GPS coin-cell load: V_BCKP=VCC. VCC_RF unused; antenna bias from protected AUX rail.')
c('C_GPS1','100n',115,51,'+3V3_AUX','GND','gps',90)
c('C_GPS2','10u',105,56,'+3V3_AUX','GND','gps')   # moved west in Rev F: the new J14 sits at (110,61)
part('J_ANT','GPS SMA FEMALE','Connector_Coaxial:SMA_Amphenol_901-143_Horizontal',125,24.5,{1:'RF_ANT',2:'GND'},
     '901-143-6RFX','Amphenol RF','gps',notes='Active GPS antenna 3.3V bias, never WiFi. Brass-body variant of the Amphenol 901-143: same PCB hole pattern (1x 1.5mm + 4x 1.7mm at +/-2.54mm) and same 50 ohm SMA interface, ~1/3 the price. Supplier drawing/board-edge fit must be inspected (body is ~0.65mm longer).')
part('C_RF','100pF C0G','Capacitor_SMD:C_0402_1005Metric',125,33,{1:'RF_ANT',2:'RF_GNSS'},'GRM1555C1H101JA01D','Murata','gps',270)
part('L_RF','27nH RF choke','Inductor_SMD:L_0402_1005Metric',131,29,{1:'RF_ANT',2:'ANT_BIAS'},'LQG15HS27NJ02D','Murata','gps')
part('D_RF','RF ESD','Diode_SMD:D_SOD-882',122,28,{1:'RF_ANT',2:'GND'},'PESD5V0F1BL,315','Nexperia','gps',notes='BOM-REV1: 5.5V standoff / 0.4pF bidirectional (PESD3V3U1UL was 3.3V/2.6pF and is out of stock). Same SOD-882/DFN1006-2 footprint; antenna bias is 3.3V so the higher standoff is also correct. Verify capacitance/S-parameter budget at GNSS bands.')
part('U_ANT','TPS2553','Package_TO_SOT_SMD:SOT-23-6',140,34,
 {1:'+3V3_AUX',2:'GND',3:'+3V3_AUX',5:'+3V3_AUX',6:'ANT_SWITCH'},'TPS2553DBVR','Texas Instruments','gps',
 names={1:'IN',2:'GND',3:'EN',4:'FAULT_N',5:'ILIM',6:'OUT'},types={1:'power_in',6:'power_out'},
 notes='ILIM tied to IN: 50/75/100mA min/typ/max limit per datasheet. Antenna fed from AUX, not GPS VCC_RF.')
# FAULT_N left unconnected; no firmware fault-monitoring claim.
part('R_ANT_FILTER','10R 1%','Resistor_SMD:R_0805_2012Metric',137,28,{1:'ANT_SWITCH',2:'ANT_BIAS'},'RC0805FR-0710RL','Yageo','gps')
c('C_ANT_IN','100n',140,40,'+3V3_AUX','GND','gps')
c('C_ANT_OUT','100n',143,27,'ANT_BIAS','GND','gps',90)
# Cross-domain GPS UART isolation: AUX-supplied TX buffer, MCU-supplied RX Schmitt.
part('U_GTX','SN74LVC1G125',SOT5,111,34,{1:'GND',2:'GPS_TX_MCU',3:'GND',4:'GPS_RX_AUX',5:'+3V3_AUX'},
 'SN74LVC1G125DBVR','Texas Instruments','gps',names={1:'OE_N',2:'A',3:'GND',4:'Y',5:'VCC'},types={2:'input',4:'tri_state',5:'power_in'})
c('C_GTX','100n',107,32,'+3V3_AUX','GND','gps')

# 05: conditioned tach only; 202k DC input, comparator + locally powered opto LED.
part('J_TACH','TACH INPUT',TFP(2),30,32,{1:'TACH_IN',2:'GND'},'1729018','Phoenix Contact','tach',270,
 names={1:'SIGNAL',2:'RETURN'},notes='ECU/cluster conditioned 3.3/5/battery-level waveform ONLY. NEVER coil or injector.')
r('R_TTOP','180k',39,28,'TACH_IN','TACH_SENSE','tach')
r('R_TBOT','22k',43,28,'TACH_SENSE','GND','tach',90)
c('C_TFILTER','1n',47,28,'TACH_SENSE','GND','tach',90)
clamp('D_TPOS','2.5',41,34,'TACH_SENSE','tach')
schottky('D_TNEG',37,36,'GND','TACH_SENSE','tach')
part('U_CMP','LM2903B',SO8,51,42,
 {1:'TACH_CMP',2:'TACH_REF',3:'TACH_SENSE',4:'GND',5:'GND',6:'TACH_REF',8:'+5V_MAIN'},
 'LM2903BIDR','Texas Instruments','tach',names={1:'OUT1',2:'IN1-',3:'IN1+',4:'GND',5:'IN2+',6:'IN2-',7:'OUT2',8:'VCC'},
 types={1:'open_collector',2:'input',3:'input',5:'input',6:'input',7:'open_collector',8:'power_in'})
r('R_TREFH','240k',54,34,'+5V_MAIN','TACH_REF','tach',90)
r('R_TREFL','10k',55,38,'TACH_REF','GND','tach')
c('C_TREF','100n',55,47,'TACH_REF','GND','tach')
c('C_CMP','100n',48,47,'+5V_MAIN','GND','tach')
r('R_THYST','2.2M',45,42,'TACH_CMP','TACH_SENSE','tach',90)
r('R_TPULL','4.7k',41,46,'+5V_MAIN','TACH_CMP','tach')
r('R_TBASE','10k',39,50,'TACH_CMP','TACH_BASE','tach')
r('R_TBE','100k',45,51,'TACH_BASE','GND','tach')
part('Q_TLED','MMBT3904',SOT3,50,53,{1:'TACH_BASE',2:'GND',3:'TACH_LED_K'},'MMBT3904,215','Nexperia','tach',names={1:'B',2:'E',3:'C'})
r('R_TLED','680',55,52,'+5V_MAIN','TACH_LED_A','tach')
part('U_OPTO','VO617A-3','Package_DIP:SMDIP-4_W9.53mm',47,60,
 {1:'TACH_LED_A',2:'TACH_LED_K',3:'GND',4:'TACH_OPTO'},'VO617A-3X017T','Vishay','tach',
 names={1:'LED A',2:'LED K',3:'E',4:'C'},types={4:'open_collector'},notes='100-200% CTR grade; ground shared, not whole-system galvanic isolation.')
r('R_TOPULL','4.7k',55,65,'+3V3_MCU','TACH_OPTO','tach')
part('U_INPUT','SN74LVC2G17',SOT6,88,57,{1:'TACH_OPTO',2:'GND',3:'GPS_TX_AUX',4:'GPS_RX_MCU',5:'+3V3_MCU',6:'TACH_MCU'},
 'SN74LVC2G17DBVR','Texas Instruments','tach',names={1:'1A',2:'GND',3:'2A',4:'2Y',5:'VCC',6:'1Y'},
 types={1:'input',3:'input',4:'output',5:'power_in',6:'output'},notes='Inputs tolerate 5.5V, Ioff isolation; GPS AUX cannot back-power MCU through this buffer.')
c('C_INPUT','100n',88,52,'+3V3_MCU','GND','tach')

# 06: oil/TPS/brake/AEM/NTC, rail-independent clamps and powered-off ADC isolation.
# Each external 0.5-4.5V transducer gets its own PTC-fused 5V branch so one shorted
# sensor cannot take out the others.
part('J_OIL','OIL 0.5-4.5V',TFP(3),30,46,{1:'+5V_SENSOR',2:'GND',3:'OIL_SIGNAL'},'1729021','Phoenix Contact','analog',270,
 names={1:'5V OUT',2:'RETURN',3:'SIGNAL'})
part('F_SENSOR','100mA PTC','Fuse:Fuse_1206_3216Metric',40,60,{1:'+5V_MAIN',2:'+5V_SENSOR'},'1206L010/60WR','Littelfuse','analog',notes='BOM-REV1: 1206L010/30YR does not exist in the 1206L series; the 0.10A part is 1206L010/60WR (60V) in the same 1206 package.')
part('J_TPS','THROTTLE 0.5-4.5V',TFP(3),30,93,{1:'+5V_TPS',2:'GND',3:'TPS_SIGNAL'},'1729021','Phoenix Contact','analog',270,
 names={1:'5V OUT',2:'RETURN',3:'SIGNAL'})
part('F_TPS','100mA PTC','Fuse:Fuse_1206_3216Metric',146,34,{1:'+5V_MAIN',2:'+5V_TPS'},'1206L010/60WR','Littelfuse','analog',notes='BOM-REV1: 1206L010/30YR does not exist in the 1206L series; the 0.10A part is 1206L010/60WR (60V) in the same 1206 package.')
part('J_BRAKE','BRAKE 0.5-4.5V',TFP(3),30,112,{1:'+5V_BRK',2:'GND',3:'BRK_SIGNAL'},'1729021','Phoenix Contact','analog',270,
 names={1:'5V OUT',2:'RETURN',3:'SIGNAL'})
part('F_BRK','100mA PTC','Fuse:Fuse_1206_3216Metric',146,56,{1:'+5V_MAIN',2:'+5V_BRK'},'1206L010/60WR','Littelfuse','analog',notes='BOM-REV1: 1206L010/30YR does not exist in the 1206L series; the 0.10A part is 1206L010/60WR (60V) in the same 1206 package.')
part('J_COOL','COOLANT NTC',TFP(2),30,65,{1:'NTC_SIGNAL',2:'GND'},'1729018','Phoenix Contact','analog',270,names={1:'NTC',2:'RETURN'})
part('J_AFR','AEM 30-0300 ONLY',TFP(2),30,79,{1:'AFR_SIGNAL',2:'GND'},'1729018','Phoenix Contact','analog',270,
 names={1:'WHITE SIG+',2:'BROWN RETURN'},notes='External AEM gauge only. Gauge supply/heater NOT powered here.')
# Sixth channel (Rev F): the car's own 12 V input, not a 0.5-4.5 V sensor. Taps the
# post-fuse/post-TVS VIN_FUSED node (the fuse drop is ~10 mV, and the node is the one
# the SMBJ20CA already clamps), divides by 10 (180k/20k) so 6-20 V lands at 0.6-2.0 V,
# and saturates at the 3.0 V shunt for anything above ~30 V. Same protection contract
# as the other five: independent LM4040 + Schottky clamp, 1k/100n filter, TMUX1511
# powered-off isolation. Ratio differs BY DESIGN - it is not a 0.500-gain channel.
for prefix,source,top,bottom,x,y,cl in [('OIL','OIL_SIGNAL','20k','20k',41,66,'3.0'),('AFR','AFR_SIGNAL','20k','20k',41,90,'3.0'),('NTC','NTC_SIGNAL','20k','20k',43,78,'3.0'),('TPS','TPS_SIGNAL','20k','20k',152,40,'3.0'),('BRK','BRK_SIGNAL','20k','20k',152,62,'3.0'),('VINMON','VIN_FUSED','180k','20k',131,72,'3.0')]:
 raw=prefix+'_CLAMP'; filt=prefix+'_FILTER'
 r('R_'+prefix+'H',top,x,y,source,raw,'analog',precision=True)
 r('R_'+prefix+'L',bottom,x+6,y,raw,'GND','analog',90,precision=True)
 clamp('D_'+prefix+'P',cl,x+10,y+4,raw,'analog')
 schottky('D_'+prefix+'N',x+2,y+5,'GND',raw,'analog')
 r('R_'+prefix+'F','1k',x+8,y+9,raw,filt,'analog')
 c('C_'+prefix+'F','100n',x+13,y+9,filt,'GND','analog',90)
# 4.096V excitation, independent shunt sinks positive external faults, diode blocks
# fault current entering the 5V rail. Two extra 20k divider resistors load the NTC
# by 40k; the revised conversion MUST compensate that parallel conductance.
schottky('D_NTC_SUP',89,69,'+5V_MAIN','NTC_FEED','analog')
r('R_NTC_BIAS','150',89,74,'NTC_FEED','NTC_EXC','analog')
clamp('U_NTC_REF','4.1',94,79,'NTC_EXC','analog')
# Exact TI output suffix is 4.1 (nominal 4.096V), not 4.096.
c('C_NTC_EXC','100n',89,81,'NTC_EXC','GND','analog')
r('R_NTC_PU','2.49k',97,86,'NTC_EXC','NTC_SIGNAL','analog',precision=True)
part('U_ADC','TMUX1511', 'Package_SO:TSSOP-14_4.4x5mm_P0.65mm',87,94,
 {1:'+3V3_MCU',2:'OIL_FILTER',3:'OIL_ADC',4:'+3V3_MCU',5:'AFR_FILTER',6:'AFR_ADC',7:'GND',8:'NTC_ADC',9:'NTC_FILTER',10:'+3V3_MCU',11:'VINMON_ADC',12:'VINMON_FILTER',13:'+3V3_MCU',14:'+3V3_MCU'},
 'TMUX1511PWR','Texas Instruments','analog',names={1:'SEL1',2:'S1',3:'D1',4:'SEL2',5:'S2',6:'D2',7:'GND',8:'D3',9:'S3',10:'SEL3',11:'D4',12:'S4',13:'SEL4',14:'VDD'},
 types={1:'input',4:'input',10:'input',13:'input',14:'power_in'},notes='Power-off protection ONLY to 3.6V: independent shunts limit S1-S4 below 3.6V. Channel 4 = car input voltage monitor (VINMON), routed to Teensy A16/pin 40.')
c('C_ADC','100n',95,94,'+3V3_MCU','GND','analog')
# Second TMUX1511 for the added throttle and brake channels. Same protection
# contract as channels 1-3: the LM4040 shunt must hold S1/S2 below 3.6V.
part('U_ADC2','TMUX1511','Package_SO:TSSOP-14_4.4x5mm_P0.65mm',156,90,
 {1:'+3V3_MCU',2:'TPS_FILTER',3:'TPS_ADC',4:'+3V3_MCU',5:'BRK_FILTER',6:'BRK_ADC',7:'GND',12:'GND',13:'GND',14:'+3V3_MCU'},
 'TMUX1511PWR','Texas Instruments','analog',names={1:'SEL1',2:'S1',3:'D1',4:'SEL2',5:'S2',6:'D2',7:'GND',8:'D3',9:'S3',10:'SEL3',11:'D4',12:'S4',13:'SEL4',14:'VDD'},
 types={1:'input',4:'input',10:'input',13:'input',14:'power_in'},
 notes='Channels 1-2 = throttle/brake. Channels 3-4 unused. Powered-off isolation only to 3.6V; shunts are the protection.')
c('C_ADC2','100n',163,90,'+3V3_MCU','GND','analog')

# 07: screen powered J10, fixed-direction dual-supply translators with Ioff.
part('J2','SCREEN J10 ONLY',TFP(4),30,145,{1:'GND',2:'+5V_SCREEN',3:'DASH_RX_CABLE',4:'DASH_TX_CABLE'},
 '1729034','Phoenix Contact','screen',270,names={1:'GND',2:'5V OUT',3:'RX FROM',4:'TX TO'})
for ref,x,y,direction,aa,bb in [('U4',46,120,'+3V3_MCU','DASH_TX_MCU','DASH_TX_5V'),('U5',54,125,'GND','DASH_RX_MCU','DASH_RX_5V')]:
 part(ref,'SN74LVC1T45',SOT6,x,y,{1:'+3V3_MCU',2:'GND',3:aa,4:bb,5:direction,6:'+5V_SCREEN'},
 'SN74LVC1T45DBVR','Texas Instruments','screen',names={1:'VCCA',2:'GND',3:'A',4:'B',5:'DIR',6:'VCCB'},types={1:'power_in',6:'power_in',5:'input'})
 c('C_'+ref+'A','100n',x-3,y-4,'+3V3_MCU','GND','screen')
 c('C_'+ref+'B','100n',x+3,y+4,'+5V_SCREEN','GND','screen')
r('R_DTX','100',42,146,'DASH_TX_5V','DASH_TX_CABLE','screen')
r('R_DRX','100',41,138,'DASH_RX_CABLE','DASH_RX_5V','screen')
r('R_DTIDLE','47k',54,134,'+3V3_MCU','DASH_TX_MCU','screen')
r('R_DRIDLE','47k',50,141,'+5V_SCREEN','DASH_RX_CABLE','screen')
for ref,y,n in [('D_DTX',149,'DASH_TX_CABLE'),('D_DRX',142,'DASH_RX_CABLE')]:
 part(ref,'UART ESD','Diode_SMD:D_SOD-323',37,y,{1:n,2:'GND'},'PESD5V0S1BA,115','Nexperia','screen')

# 08: independent internal-antenna WiFi, buffered SPI, open-drain reset and UART service.
part('U_NET','ESP32-S3-WROOM-1-N8R2','RF_Module:ESP32-S3-WROOM-1',68,162.25,
 {1:'GND',2:'+3V3_NET',3:'NET_EN',18:'NET_CS',19:'NET_MOSI',20:'NET_SCK',21:'NET_MISO',22:'NET_IRQ',27:'NET_BOOT',36:'NET_UART_RX',37:'NET_UART_TX',40:'GND',41:'GND'},
 'ESP32-S3-WROOM-1-N8R2','Espressif','wifi',180,
 names={1:'GND',2:'3V3',3:'EN',18:'GPIO10 CS',19:'GPIO11 MOSI',20:'GPIO12 SCK',21:'GPIO13 MISO',22:'GPIO14 IRQ',27:'GPIO0 BOOT',36:'GPIO44 RXD0',37:'GPIO43 TXD0',40:'GND',41:'EP GND'},types={2:'power_in',3:'input',36:'input',37:'output'},
 notes='PCB antenna over carrier notch; ALL-LAYER keepout. New coprocessor firmware required. No WiFi external antenna.')
c('C_NETB','10u',81,139,'+3V3_NET','GND','wifi',90)
c('C_NETD','100n',81,147,'+3V3_NET','GND','wifi',90)
r('R_NETEN','10k',89,132,'+3V3_NET','NET_EN','wifi')
c('C_NETEN','1u',96,132,'NET_EN','GND','wifi')
r('R_NETBOOT','10k',93,126,'+3V3_NET','NET_BOOT','wifi')
part('Q_NETRESET','MMBT3904',SOT3,101,127,{1:'NET_RESET_BASE',2:'GND',3:'NET_EN'},'MMBT3904,215','Nexperia','wifi',names={1:'B',2:'E',3:'C'})
r('R_NETRB','10k',107,129,'NET_RESET_ASSERT','NET_RESET_BASE','wifi')
r('R_NETRPD','100k',107,134,'NET_RESET_BASE','GND','wifi')
# Quad buffer = three master signals; fourth unused. NET supply, Ioff/5V tolerant inputs.
part('U_SPI_TX','SN74LVC125A','Package_SO:TSSOP-14_4.4x5mm_P0.65mm',88,117,
 {1:'GND',2:'NET_CS_MCU',3:'NET_CS_PRE',4:'GND',5:'NET_MOSI_MCU',6:'NET_MOSI_PRE',7:'GND',8:'NET_SCK_PRE',9:'NET_SCK_MCU',10:'GND',12:'GND',13:'+3V3_NET',14:'+3V3_NET'},
 'SN74LVC125APWR','Texas Instruments','wifi',names={1:'1OE_N',2:'1A',3:'1Y',4:'2OE_N',5:'2A',6:'2Y',7:'GND',8:'3Y',9:'3A',10:'3OE_N',11:'4Y',12:'4A',13:'4OE_N',14:'VCC'},types={14:'power_in',3:'tri_state',6:'tri_state',8:'tri_state',11:'tri_state'})
c('C_SPITX','100n',87,109,'+3V3_NET','GND','wifi')
for n,x,y in [('CS',82,124),('MOSI',77,127),('SCK',72,130)]: r('R_NET'+n,'33',x,y,'NET_'+n+'_PRE','NET_'+n,'wifi')
r('R_CS_IDLE','10k',81,99,'+3V3_MCU','NET_CS_MCU','wifi')
r('R_SCK_IDLE','47k',100,100,'NET_SCK_MCU','GND','wifi')
part('U_SPI_RX','SN74LVC2G125','Package_SO:VSSOP-8_2.3x2mm_P0.5mm',106,99,
 {1:'GND',2:'NET_MISO',3:'NET_MISO_MCU',4:'GND',5:'NET_IRQ_MCU',6:'NET_IRQ',7:'GND',8:'+3V3_MCU'},
 'SN74LVC2G125DCUR','Texas Instruments','wifi',names={1:'1OE_N',2:'1A',3:'1Y',4:'GND',5:'2Y',6:'2A',7:'2OE_N',8:'VCC'},types={8:'power_in',3:'tri_state',5:'tri_state'})
c('C_SPIRX','100n',111,95,'+3V3_MCU','GND','wifi')
r('R_IRQ_IDLE','47k',112,102,'NET_IRQ','GND','wifi')
r('R_MISO_IDLE','47k',117,99,'NET_MISO','GND','wifi')
part('J_NET_SERVICE','NET 3V3 UART SERVICE','Connector_PinHeader_2.54mm:PinHeader_2x03_P2.54mm_Vertical',135,122,
 {1:'GND',2:'+3V3_NET',3:'NET_UART_TX',4:'NET_UART_EXT_RX',5:'NET_BOOT',6:'NET_EN'},'TSW-103-07-G-D','Samtec','wifi',
 notes='1GND 2 3V3 REFERENCE OUT 3TX fromNET 4RX toNET 5BOOT0 6EN. 5/6 open-drain or ground ONLY. Do NOT power via pin2.')
part('U_NETUART','SN74LVC1G125',SOT5,135,134,{1:'GND',2:'NET_UART_EXT_RX',3:'GND',4:'NET_UART_RX',5:'+3V3_NET'},
 'SN74LVC1G125DBVR','Texas Instruments','wifi',types={5:'power_in',4:'tri_state'},notes='Prevents powered USB-UART TX backfeeding an unpowered NET rail')
c('C_NETUART','100n',143,134,'+3V3_NET','GND','wifi')

# Output identity and actual connectivity. No fabricated review approval is emitted.
for i,(x,y) in enumerate([(24,24),(166,24),(24,171),(166,171)],1):
 part('H'+str(i),'M3 MOUNT','MountingHole:MountingHole_3.2mm_M3',x,y,{},'M3 insulated hardware','Assembly','mechanical')
for i,(x,y,n) in enumerate([(40,118,'GND'),(97,106,'+5V_MAIN'),(112,83,'+3V3_AUX'),(133,114,'+3V3_NET'),(82,47,'+3V3_MCU')],1):
 part('TP'+str(i),n,'TestPoint:TestPoint_THTPad_D2.0mm_Drill1.0mm',x,y,{1:n},'PCB test pad','PCB','mechanical')
# Placement refinement after a DRC pass (all changes also update the source inventory).
for ref,x,y in [('J_TACH',30,32),('D_TNEG',40.5,39),('F_SENSOR',40,60),
 ('L_RF',128,30),('U_OPTO',96,59),('R_TOPULL',99,65),('D_RF',125,29.8),('C_ANT_OUT',143,30),
 ('U_ADC',129,87),('C_ADC',136,86),('U_SPI_TX',134,102),
 ('C_SPITX',141,100),('R_CS_IDLE',100,91),('R_NETCS',132,112),('R_NETMOSI',135,109),('R_NETSCK',140,107),('D1',48,108),('F1',40.5,116),('TP1',55,151),('TP4',143,116),('R_NETBOOT',99,131),('C_NETEN',94,132),('D_AFRP',53,97),('D_AFRN',47,103),('R_AFRF',54,104),('C_AFRF',56,99),('U_INPUT',85,57),('D_DTX',40,150),('D_DRX',40,143),
 ('R_DTIDLE',52,137),('R_DRX',44,140),('C_U4A',42,121),('C_U4B',50,122),('C_U5A',50,126),('C_U5B',59,123)]:
    FPS[ref].SetPosition(P(x,y))
    p=next(p for p in PARTS if p['ref']==ref);p['x']=x;p['y']=y


def line(a,b,layer=k.Edge_Cuts,width=.05):
 ob=k.PCB_SHAPE(B);ob.SetShape(k.SHAPE_T_SEGMENT);ob.SetStart(P(*a));ob.SetEnd(P(*b));ob.SetLayer(layer);ob.SetWidth(MM(width));B.Add(ob)
# Rev F envelope 150 x 155 mm. The left edge grew so all eight field-wired screw
# terminals fit on one accessible edge; the extra width on the right carries the
# added throttle/brake channels and the Pi video UART.
# WiFi antenna moved to x68; stock keepout spans x44..92, y169..190.
outline=[(20,20),(170,20),(170,175),(92,175),(92,169),(44,169),(44,175),(20,175),(20,20)]
for a,b in zip(outline,outline[1:]):line(a,b)
def text(s,x,y,size=.9):
 t=k.PCB_TEXT(B);t.SetText(s);t.SetPosition(P(x,y));t.SetTextSize(P(max(.8,size),max(.8,size)));t.SetTextThickness(MM(.13));t.SetLayer(k.F_SilkS);B.Add(t)
text('RACECAR-35 REV F',150,150,1.0);text('ENGINEERING PROTOTYPE',150,153,.8)
# CAN island: J14 pinout on the silkscreen and the bus-end rule next to the jumper.
# CAN island: J14's pins run LEFT->RIGHT (pad 1 is the square pad), so each label sits
# directly BELOW ITS OWN pad. A vertical label stack beside a horizontal pin row is read
# as pin 1 = top and miswires CANH/CANL.
for _x,lab in [(110.0,'1 CANH'),(115.08,'2 CANL'),(120.16,'3 GND')]:text(lab,_x,67.3,.5)
text('CAN TERM FIT ONLY AT BUS END',146,52.0,.55)
text('CUT TEENSY VUSB-VIN',74,23,.8);text('GPS SMA',160,23,.8)
for ref,title,labels in [('J_TACH','TACH',['1 SIG','2 RET']),('J_OIL','OIL',['1 +5V','2 RET','3 SIG']),('J_COOL','NTC',['1 NTC','2 RET']),('J_AFR','AEM AFR',['1 WHT+','2 BRN-']),('J_TPS','THROTTLE',['1 +5V','2 RET','3 SIG']),('J_BRAKE','BRAKE',['1 +5V','2 RET','3 SIG']),('J1','10-20V IN',['1 VIN+','2 GND']),('J2','SCREEN J10',['1 GND','2 +5V','3 RX','4 TX'])]:
 y=k.ToMM(FPS[ref].GetPosition().y);text(title,38,y-(7.5 if ref=='J_TACH' else 3.0 if ref=='J_OIL' else 5.5),.8)
 for i,lab in enumerate(labels):text(lab,38,y+i*5.08+(2.0 if ref=='J1' and i==1 else 0),.8)
text('NO COIL / INJECTOR',58,22,.65);text('RTC 3V NO CHARGE',120,171,.75)
text('INTERNAL WIFI',68,147,.8);text('3V3 NET',136,104,.8)
text('PI 5 VIDEO UART 3V3',152,100,.5)
B.GetTitleBlock().SetTitle('Racecar integrated controller'); B.GetTitleBlock().SetRevision('F')
ds=B.GetDesignSettings();ds.m_TrackMinWidth=MM(.15);ds.m_MinClearance=MM(.15);ds.m_CopperEdgeClearance=MM(.3);ds.m_HoleClearance=MM(.25);ds.m_SilkClearance=MM(.1)
# Manufacturing/EDA references must have numeric suffixes; keep readable aliases
# separately. Named-only references such as U_GPS pass some ERC paths but block
# KiCad netlist export as unannotated symbols.
import re
aliases={'J1':'J1','J2':'J2','J_TACH':'J3','J_OIL':'J4','J_COOL':'J5','J_AFR':'J6','J_ANT':'J8','J_BAT':'J9','J_NET_SERVICE':'J10','J_TPS':'J11','J_BRAKE':'J12','J_VID':'J13','J14':'J14',
 # Rev F onboard CAN island (J7 and its SN65HVD230 module are deleted).
 # U21 keeps its own number; the rest are pinned to the first free high numbers so
 # no existing reference designator is renumbered and the PCBWay documents stay valid.
 'U21':'U21','JP2':'JP2','TP6':'TP6','TP7':'TP7',
 'D_U21TVS':'D25','R_T1':'R58','R_T2':'R59','C_U21V':'C49','C_U21IO':'C50','C_U21B':'C51','C_T':'C52',
 # Rev F: the six car-input-monitor parts are pinned to the first free HIGH numbers so
 # every pre-existing reference designator keeps the number it has in Rev D. The PCBWay
 # quotation, the BOM-change log and all assembly documents are keyed to those numbers,
 # so shifting the whole numbering scheme would invalidate them for no benefit.
 'R_VINMONH':'R48','R_VINMONL':'R49','R_VINMONF':'R50','D_VINMONP':'D21','D_VINMONN':'D22','C_VINMONF':'C40',
 # Rev F power stage (TPS54560B + reverse protection), same rule: pinned to free high
 # numbers so NOTHING else on the board is renumbered.
 'Q_REV':'Q3','L_MAIN':'L2','D_MAIN':'D23','D_REVZ':'D24',
 'R_REVG':'R51','R_RT':'R52','R_FBH':'R53','R_FBL':'R54','R_COMP':'R55','R_UV1':'R56','R_UV2':'R57',
 'C_IN1':'C41','C_IN2':'C42','C_BOOT':'C43','C_OUT1':'C44','C_OUT2':'C45','C_OUT3':'C46','C_COMP':'C47','C_CPOLE':'C48',
 # Rev F: deleting the old CAN-module decoupling cap (C_CAN, Rev E's C8) from the part
 # list would shift every later auto-numbered cap down by one (C9..C39 -> C8..C38) and
 # silently repoint the finishing pass and the documents at the wrong component. Pin
 # every one of those caps to the reference it already had; C8 is left vacant on purpose.
 'C_U3I':'C3','C_U3O':'C4','C_U_NET_PWRI':'C5','C_U_NET_PWRO':'C6','C_MCU':'C7',
 'C_IMUV':'C9','C_IMUL':'C10','C_IMUR':'C11','C_GPS1':'C12','C_GPS2':'C13','C_RF':'C14',
 'C_ANT_IN':'C15','C_ANT_OUT':'C16','C_GTX':'C17','C_TFILTER':'C18','C_TREF':'C19','C_CMP':'C20',
 'C_INPUT':'C21','C_OILF':'C22','C_AFRF':'C23','C_NTCF':'C24','C_TPSF':'C25','C_BRKF':'C26',
 'C_NTC_EXC':'C27','C_ADC':'C28','C_ADC2':'C29','C_U4A':'C30','C_U4B':'C31','C_U5A':'C32',
 'C_U5B':'C33','C_NETB':'C34','C_NETD':'C35','C_NETEN':'C36','C_SPITX':'C37','C_SPIRX':'C38','C_NETUART':'C39'}
used=set(aliases.values()) | {p['ref'] for p in PARTS if re.fullmatch(r'[A-Z]+[0-9]+',p['ref']) and p['ref'] not in aliases}
for p in PARTS:
    alias=p['ref'];prefix=alias.split('_')[0]
    if alias not in aliases:
        if re.fullmatch(r'[A-Z]+[0-9]+',alias):aliases[alias]=alias
        else:
            i=1
            while prefix+str(i) in used:i+=1
            aliases[alias]=prefix+str(i);used.add(aliases[alias])
    p['alias']=alias;p['ref']=aliases[alias];FPS[alias].SetReference(p['ref'])
(D/'COMPONENT-ALIASES.json').write_text(json.dumps(aliases,indent=2)+'\n')
k.SaveBoard(str(D/(NAME+'.kicad_pcb')),B)
project={'meta':{'filename':NAME+'.kicad_pro','version':1},'board':{'design_settings':{'rules':{'min_clearance':.15,'min_track_width':.15,'min_via_diameter':.6,'min_through_hole_diameter':.3,'min_copper_edge_clearance':.3}}},'net_settings':{'classes':[{'name':'Default','clearance':.15,'track_width':.2,'via_diameter':.7,'via_drill':.3,'microvia_diameter':.3,'microvia_drill':.1,'diff_pair_width':.25,'diff_pair_gap':.25,'diff_pair_via_gap':.25,'bus_width':12,'line_style':0,'pcb_color':'rgba(0, 0, 0, 0.000)','schematic_color':'rgba(0, 0, 0, 0.000)','wire_width':6}], 'meta':{'version':4},'netclass_assignments':{},'netclass_patterns':[]}}
(D/(NAME+'.kicad_pro')).write_text(json.dumps(project,indent=2)+'\n')
(D/'fp-lib-table').write_text('(fp_lib_table (version 7) (lib (name "RacecarC")(type "KiCad")(uri "${KIPRJMOD}/RacecarC.pretty")(options "")(descr "Rev F selected footprints")))\n')
(ROOT/'components.json').write_text(json.dumps(PARTS,indent=2)+'\n')
with (D/'BOM.csv').open('w') as f:
 w=csv.writer(f);w.writerow(['Reference','Function','Value','Manufacturer','MPN','Footprint','Notes'])
 for p in PARTS:w.writerow([p['ref'],p['alias'],p['value'],p['maker'],p['mpn'],p['footprint'],p['notes']])
print('Generated electrical PCB:',len(PARTS),'parts;',len(NET),'nets. UNROUTED; not fabrication ready.')

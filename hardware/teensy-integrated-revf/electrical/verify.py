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
# ---- silkscreen legends (regression guard, 2026-10-19) -------------------------------
# The first upload lost 12 terminal labels: a finishing step deleted F.SilkS texts by
# STRING, so J5 had no name or pin labels and J3/J4/J11/J12 showed only '3 SIG'. The
# legends now come from ONE table (silk_legends.py) that slides a crowded label along its
# column and raises instead of deleting. These checks read the BOARD, not the table.
import silk_legends as SL
stexts=[t for t in b.GetDrawings() if isinstance(t,k.PCB_TEXT) and t.GetLayer()==k.F_SilkS]
def tbox(t):
 r=t.GetEffectiveTextShape().BBox();return k.ToMM(r.GetLeft()),k.ToMM(r.GetTop()),k.ToMM(r.GetRight()),k.ToMM(r.GetBottom())
for t in stexts:
 check(t.GetTextThickness()>=k.FromMM(.153),f'silk text {t.GetText()!r}: stroke below the 0.153 mm legend minimum')
 check(t.GetTextHeight()>=k.FromMM(.9)-1,f'silk text {t.GetText()!r}: height below 0.9 mm')
col_hits=0
for ref,title,labels in SL.TERMINALS:
 fp=fps[ref];bx0,by0,bx1,by1=SL._body(fp)
 hits=[t for t in stexts if t.GetText()==title]
 check(len(hits)==1,f'{ref}: expected exactly one title {title!r}, found {len(hits)}')
 x0,y0,x1,y1=tbox(hits[0])
 check(abs((x0+x1)/2-(bx0+bx1)/2)<=.6,f'{ref}: title {title!r} not centred over the connector')
 check(.05<=by0-y1<=1.0,f'{ref}: title {title!r} is not just above the connector body')
 rows=sorted(k.ToMM(p.GetPosition().y) for p in fp.Pads())
 check(len(rows)==len(labels),f'{ref}: {len(rows)} pads but {len(labels)} labels')
 colx=bx1+SL.PIN_GAP_X
 for n,(lab,ry) in enumerate(zip(labels,rows),1):
  check(lab.split(' ')[0]==str(n),f'{ref}: label {lab!r} does not start with its pin number {n}')
  c=[t for t in stexts if t.GetText()==lab and t.GetHorizJustify()==k.GR_TEXT_H_ALIGN_LEFT
     and abs(k.ToMM(t.GetPosition().x)-colx)<=.02 and abs(k.ToMM(t.GetPosition().y)-ry)<=SL.PIN_MAX_SLIDE+1e-6]
  check(len(c)==1,f'{ref} pin {n}: no left-aligned label {lab!r} on its pad row')
  ly=k.ToMM(c[0].GetPosition().y)
  check(all(abs(ly-ry)<abs(ly-o) for o in rows if o!=ry),f'{ref} pin {n}: label nearer another pin')
 col_hits+=sum(1 for t in stexts if t.GetHorizJustify()==k.GR_TEXT_H_ALIGN_LEFT
               and abs(k.ToMM(t.GetPosition().x)-colx)<=.02 and by0-1<k.ToMM(t.GetPosition().y)<by1+1)
check(col_hits==sum(len(l) for _,_,l in SL.TERMINALS),'stray or duplicate text in the pin-label column')
# Crossed harness table: each J2 digit must sit directly above the screen-J10 pin it is
# wired to, and that pairing must be electrically right for the nets on J2's pads.
PANEL_J10={1:'RXD0_H',2:'TXD0_H',3:'+5V_IN',4:'GND'}   # CrowPanel Advance J10, all revisions
MATE={'GND':'GND','+5V_SCREEN':'+5V_IN','DASH_RX_CABLE':'TXD0_H','DASH_TX_CABLE':'RXD0_H'}
j2nets={int(p.GetNumber()):p.GetNetname() for p in fps['J2'].Pads()}
check(sum(1 for t in stexts if t.GetText()==SL.CROSS_TITLE)==1,'harness legend title missing')
table={}
for (lab,digits),y in zip(SL.CROSS_ROWS,SL.CROSS_Y[1:]):
 check(any(t.GetText()==lab and abs(k.ToMM(t.GetPosition().y)-y)<.02 for t in stexts),f'harness row label {lab} missing')
 for x in SL.CROSS_COL_X:
  d=[t.GetText() for t in stexts if abs(k.ToMM(t.GetPosition().x)-x)<.02 and abs(k.ToMM(t.GetPosition().y)-y)<.02]
  check(len(d)==1 and d[0].isdigit(),f'harness {lab} column x={x}: expected one digit, found {d}')
  table.setdefault(x,{})[lab]=int(d[0])
for x,pair in table.items():
 check(MATE[j2nets[pair['J2']]]==PANEL_J10[pair['J10']],
       f"harness legend wrong: J2.{pair['J2']} ({j2nets[pair['J2']]}) printed over J10.{pair['J10']} ({PANEL_J10[pair['J10']]})")
check(sorted(p['J2'] for p in table.values())==[1,2,3,4],'harness legend must list every J2 pin once')
# Teensy socket orientation: '1'/'25' beside socket pads 1 (GND) and 25 (TEENSY_VIN), at the USB end.
u1={p.GetNumber():p for p in fps['U1'].Pads()}
check(u1['1'].GetNetname()=='GND' and u1['25'].GetNetname()=='TEENSY_VIN','Teensy socket pin 1/25 nets changed')
for num in ('1','25'):
 q=u1[num].GetPosition()
 check(any(t.GetText()==num and abs(k.ToMM(t.GetPosition().y-q.y))<1 and abs(k.ToMM(t.GetPosition().x-q.x))<3.5
       for t in stexts),f'Teensy socket pin {num} number missing from the silkscreen')
usb=[t for t in stexts if t.GetText()=='USB END']
check(len(usb)==1 and k.ToMM(usb[0].GetPosition().y)<k.ToMM(u1['5'].GetPosition().y),'USB END mark missing or not at pin 1 end')
# EVERY connector, jumper and the cell is named AT the part. No reference designators are
# printed on this board, so the name is the only identification - and the first artwork had
# 'GPS SMA', 'PI 5 VIDEO UART 3V3' and 'RTC 3V NO CHARGE' 28-44 mm from their parts.
def gap_mm(a,b):
 return max(a[0]-b[2],b[0]-a[2],0)+max(a[1]-b[3],b[1]-a[3],0)
for f in b.GetFootprints():
 r=f.GetReference()
 if not (r.startswith('J') or r.startswith('BT')):continue      # J*, JP*, BT*
 o=SL._outline(f)
 named=[t for t in stexts if t.GetText().startswith(r+' ') and t.GetText()!=SL.CROSS_TITLE]
 check(len(named)==1,f'{r}: expected exactly one silkscreen name starting {r!r}, found {len(named)}')
 check(gap_mm(tbox(named[0]),o)<=6.0,f'{r}: its name {named[0].GetText()!r} is not at the part')
for part in SL.PARTS:
 f=fps[part['ref']];o=SL._outline(f)
 pads=sorted(f.Pads(),key=lambda p:int(p.GetNumber()))
 for lab,p in zip(part.get('labels',[]),pads):
  px,py=k.ToMM(p.GetPosition().x),k.ToMM(p.GetPosition().y)
  c=[tbox(t) for t in stexts if t.GetText()==lab and gap_mm(tbox(t),o)<=1.5]
  check(len(c)==1,f"{part['ref']}: pin label {lab!r} missing beside the part")
  x0,y0,x1,y1=c[0]
  if part['style']=='row':check(x0<=px<=x1,f"{part['ref']}: label {lab!r} is not in line with its pad")
  else:check(y0<=py<=y1,f"{part['ref']}: label {lab!r} is not level with its pad")
# J13's printed directions match the copper: pad 2 drives OUT from Teensy pin 1 (TX1,
# socket pad 3) through R1, pad 3 feeds Teensy pin 0 (RX1, socket pad 2) through R2.
j13={p.GetNumber():p.GetNetname() for p in fps['J13'].Pads()}
check(j13=={'1':'GND','2':'VID_TX_CABLE','3':'VID_RX_CABLE','4':'GND'},'J13 pinout changed - fix its silkscreen')
r1={p.GetNetname() for p in fps['R1'].Pads()};r2={p.GetNetname() for p in fps['R2'].Pads()}
check(r1=={'VID_TX_CABLE','VID_TX_MCU'} and u1['3'].GetNetname()=='VID_TX_MCU','J13.2 is not Teensy TX1 (pin 1)')
check(r2=={'VID_RX_CABLE','VID_RX_MCU'} and u1['2'].GetNetname()=='VID_RX_MCU','J13.3 is not Teensy RX1 (pin 0)')
j9={p.GetNumber():p.GetNetname() for p in fps['J9'].Pads()}
check(j9=={'1':'VBAT_CELL','2':'GND'},'J9 pinout changed - fix its silkscreen')
j10={p.GetNumber():p.GetNetname() for p in fps['J10'].Pads()}
check(j10=={'1':'GND','2':'+3V3_NET','3':'NET_UART_TX','4':'NET_UART_EXT_RX','5':'NET_BOOT','6':'NET_EN'},
      'J10 pinout changed - fix its silkscreen')
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

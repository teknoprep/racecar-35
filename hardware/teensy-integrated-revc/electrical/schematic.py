#!/usr/bin/env python3
"""Native editable Rev C schematic, with actual component pins and electrical types.
Generated from selected part/connectivity source; ERC/net equality is not circuit
simulation or independent engineering approval. Re-run after source net changes.
"""
from pathlib import Path
import json,uuid,re
ROOT=Path(__file__).resolve().parents[1]; D=ROOT/'design'; NAME='racecar-integrated-revc'
parts=json.loads((ROOT/'components.json').read_text())
def uid(s):return str(uuid.uuid5(uuid.NAMESPACE_URL,'racecar-integrated-revc/schematic/'+s))
def q(s):return json.dumps(str(s),ensure_ascii=False)
def fx(size=1,justify='',hide=False):return f'(effects (font (size {size} {size}))'+(f' (justify {justify})' if justify else '')+(' (hide yes)' if hide else '')+')'
def txt(s,x,y,size=1):return f'(text {q(s)} (at {x} {y} 0) {fx(size,"left")} (uuid {uid(s+str(x)+str(y))}))'
rootid=uid('root')
def geometry(p):
 pins=sorted(p['pins'],key=lambda n:(int(n) if n.isdigit() else 999,n))
 a=(len(pins)+1)//2; h=max(7.62,(a+1)*2.54)
 return [(n,(-17.78 if i<a else 17.78),h/2-((i if i<a else i-a)+1)*2.54,0 if i<a else 180) for i,n in enumerate(pins)],h

def symbol(p):
 ref=p['ref'];coords,h=geometry(p)
 fields=''.join(f'(property {q(k)} {q(v)} (at 0 {y} 0) {fx(1.0,hide=hidden)})' for k,v,y,hidden in [('Reference',re.sub(r'[^A-Z]','',ref),h/2+2.54,False),('Value',p['value'],-h/2-2.54,False),('Footprint',p['footprint'],0,True),('Datasheet','',0,True)])
 pins=[]
 for n,x,y,ang in coords:
  typ=p['types'][n]
  if p['ref']=='U1' and p['names'][n].startswith('P') and p['pins'][n]:typ='bidirectional'
  if p['alias']=='U_NET' and p['names'][n].startswith('GPIO'):typ='bidirectional'
  pins.append(f'(pin {typ} line (at {x} {y} {ang}) (length 5.08) (name {q(p["names"][n])} {fx(.85)}) (number {q(n)} {fx(.85)}))')
 return f'''(symbol "RevC:{ref}" (pin_names (offset 0.508)) (in_bom yes) (on_board yes) {fields}
 (symbol "{ref}_0_1" (rectangle (start -12.7 {h/2}) (end 12.7 {-h/2}) (stroke (width .254) (type default)) (fill (type background))))
 (symbol "{ref}_1_1" {''.join(pins)}))'''

def instance(p,x,y,sheet):
 x=round(x/1.27)*1.27;y=round(y/1.27)*1.27
 ref=p['ref'];coords,h=geometry(p)
 fields=''.join(f'(property {q(k)} {q(v)} (at {x} {yy} 0) {fx(size,hide=hidden)})' for k,v,yy,size,hidden in [('Reference',ref,y-h/2-2.54,1.0,False),('Value',p['value'],y+h/2+2.54,.85,False),('Footprint',p['footprint'],y,.8,True),('Datasheet','',y,.8,True)])
 bits=[f'''(symbol (lib_id "RevC:{ref}") (at {x} {y} 0) (unit 1) (in_bom yes) (on_board yes) (dnp no) (uuid {uid(ref)}) {fields}
 (instances (project {q(NAME)} (path "/{rootid}/{uid(sheet)}" (reference {q(ref)}) (unit 1)))))''']
 for n,xx,yy,ang in coords:
  ex,ey=x+xx,y-yy;net=p['pins'][n]
  if not net:
   bits.append(f'(no_connect (at {ex} {ey}) (uuid {uid(ref+"nc"+n)}))');continue
  ox=ex+(-5.08 if xx<0 else 5.08)
  bits.append(f'(wire (pts (xy {ex} {ey}) (xy {ox} {ey})) (stroke (width 0) (type default)) (uuid {uid(ref+"w"+n)}))')
  bits.append(f'(global_label {q(net)} (shape passive) (at {ox} {ey} {180 if xx<0 else 0}) {fx(.85,"right" if xx<0 else "left")} (uuid {uid(ref+"l"+n)}))')
 return '\n'.join(bits)

sheets=[]
for group in dict.fromkeys(p['group'] for p in parts):
 group_parts=[p for p in parts if p['group']==group and p['pins']]
 # Large modules get their own sheet, so no pin/label overlaps are hidden.
 for p in [p for p in group_parts if len(p['pins'])>24]:
  sheets.append((group+'-'+p['ref'],[p]));group_parts.remove(p)
 for i in range(0,len(group_parts),16):sheets.append((group+'-'+str(i//16+1),group_parts[i:i+16]))
flaglib='''(symbol "RevC:PWR_FLAG" (power) (pin_numbers hide) (pin_names (offset 0) hide) (in_bom no) (on_board no)
 (property "Reference" "#FLG" (at 0 1.905 0) (effects (font (size 1.27 1.27)) (hide yes)))
 (property "Value" "PWR_FLAG" (at 0 3.302 0) (effects (font (size 1 1))))
 (symbol "PWR_FLAG_0_1" (polyline (pts (xy 0 0) (xy 0 1.27) (xy -1.016 1.905) (xy 0 2.54) (xy 1.016 1.905) (xy 0 1.27)) (stroke (width 0) (type default)) (fill (type none))))
 (symbol "PWR_FLAG_1_1" (pin power_out line (at 0 0 90) (length 0) (name "pwr" (effects (font (size 1 1)))) (number "1" (effects (font (size 1 1)))))))'''
root=['(kicad_sch (version 20250114) (generator "eeschema") (uuid '+rootid+') (paper "A3") (lib_symbols '+flaglib+')',
 txt('RACECAR-35 INTEGRATED REV C / ELECTRICAL PROTOTYPE',20,15,2),
 txt('NOT PHYSICALLY VALIDATED. Review selected circuit, power-off behaviour, footprints and assembly before any order.',20,23,1),
 txt('Generated pin/function symbols carry electrical types; these checks are not an independent circuit review.',20,30,1)]
for i,(name,ps) in enumerate(sheets):
 x,y=22+(i%3)*130,47+(i//3)*33
 root.append(f'''(sheet (at {x} {y}) (size 115 22) (stroke (width .254) (type default)) (fill (color 0 0 0 0)) (uuid {uid(name)})
 (property "Sheetname" {q(name.upper())} (at {x} {y-1.27} 0) {fx(1,"left")})
 (property "Sheetfile" {q(name+'.kicad_sch')} (at {x} {y+23.27} 0) {fx(.85,"left")})
 (instances (project {q(NAME)} (path "/{rootid}" (page {q(i+2)})))))''')
 page=[f'(kicad_sch (version 20250114) (generator "eeschema") (uuid {uid(name)}) (paper "A3")',
       '(lib_symbols '+''.join(symbol(p) for p in ps)+')',txt('REV C / '+name.upper(),15,13,2)]
 for j,p in enumerate(ps):
  px,py=(95,110) if len(p['pins'])>24 else (54+(j%4)*97,54+(j//4)*48)
  page.append(instance(p,px,py,name))
 page += [txt('Prototype circuit; manufacturer pin names/MPNs recorded in components.json and BOM.csv.',15,262,.9),
          txt('Supply grounds shared. Analog shunts precede powered-off isolation. New oil/NTC conversion and WiFi firmware required.',15,268,.9),
          txt('NEVER coil/injector tach; NEVER screen 3V3_OUT as power input. Cut Teensy VUSB-VIN. CR2032 is not rechargeable.',15,274,.9),')']
 (D/(name+'.kicad_sch')).write_text('\n'.join(page)+'\n')
# Real sources reach these rails through passive fuse/diode/jumper components.
# ERC cannot infer that path; explicit power flags do not fabricate a supply.
for i,rail in enumerate(['VIN_FUSED','TEENSY_VIN','+5V_SCREEN']):
 x,y=round((30+i*100)/1.27)*1.27,round(245/1.27)*1.27; ref='#FLG0'+str(i+1)
 root.append(f'''(symbol (lib_id "RevC:PWR_FLAG") (at {x} {y} 0) (unit 1) (in_bom no) (on_board no) (dnp no) (uuid {uid(ref)})
 (property "Reference" "{ref}" (at {x} {y+2} 0) {fx(1,hide=True)})
 (property "Value" "SUPPLY AFTER PASSIVE PROTECTION" (at {x} {y-4} 0) {fx(.85)})
 (instances (project {q(NAME)} (path "/{rootid}" (reference "{ref}") (unit 1)))))''')
 root.append(f'(global_label {q(rail)} (shape passive) (at {x} {y} 0) {fx(.85,"left")} (uuid {uid(ref+"label")}))')
root+= [txt('ALL screw terminals face LEFT. Internal WiFi antenna. External active GPS SMA. 3V CR2032 to Teensy VBAT via removable lead.',20,267,.9),
        txt('Electrical source is under development. Do not manufacture an unrouted file or mistake ERC for physical validation.',20,275,.9),')']
(D/(NAME+'.kicad_sch')).write_text('\n'.join(root)+'\n')
(D/'RevC.kicad_sym').write_text('(kicad_symbol_lib (version 20241209) (generator "kicad_symbol_editor")\n'+''.join(symbol(p).replace('"RevC:'+p['ref']+'"','"'+p['ref']+'"',1) for p in parts if p['pins'])+flaglib.replace('"RevC:PWR_FLAG"','"PWR_FLAG"',1)+')\n')
(D/'sym-lib-table').write_text('(sym_lib_table (version 7) (lib (name "RevC")(type "KiCad")(uri "${KIPRJMOD}/RevC.kicad_sym")(options "")(descr "Rev C selected component pin/function symbols")))\n')
print('Generated real schematic:',len(sheets),'sheets;',len(parts),'selected parts')

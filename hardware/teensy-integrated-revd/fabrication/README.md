# Rev D Gerber/drill exporter

The real electrical PCB is `../design/racecar-integrated-revd.kicad_pcb`, with a
fully annotated schematic and assigned nets — distinct from the netless
`../../teensy-integrated-revc/preview/` illustration. Two intentionally different
modes exist:

```sh
# Explicit UNAPPROVED engineering-prototype files (no invented human review):
/usr/bin/python3 hardware/teensy-integrated-revd/fabrication/export.py --engineering-prototype

# Reviewed release; remains BLOCKED until a genuine independent review exists:
/usr/bin/python3 hardware/teensy-integrated-revd/fabrication/export.py
```

Engineering output is `Racecar-RevD-GERBERS-ENGINEERING-PROTOTYPE.zip`. Its
status/manifest explicitly say **no independent approval and no physical
validation**. It is not the reviewed-release `Racecar-RevD-GERBERS-PROTOTYPE.zip`.
Both modes fail closed on missing documents or actual CAD failures and remove their
own stale output before attempting export.

Common checks: genuine integrated components and routing; independent pin-contract
assertions; full-severity ERC/DRC with zero unconnected items; exact real schematic /
PCB connected-pad equality; all copper/mask/silk/paste/outline layers; separate
PTH/NPTH Excellon drills; current assembled-image identity when included; unchanged
source hashes; ZIP CRC and SHA256 manifest verification. Editable CAD, schematic
PDF, BOM/positions and the fabrication/assembly/bring-up documents travel in the ZIP.

Reviewed mode additionally requires `design-review.json`: revision D, a named actual
reviewer, `approved_for_prototype_fabrication:true`, every `REVIEWS` check genuinely
completed, and an exact `source_sha256` mapping for all design sources/documents
listed by `preflight()`. **Do not generate an approval to get past this gate.**

The engineering export path has been exercised on the routed Rev D; unit rejection
tests alone would not establish that. Independent CAM re-reading, footprint/power/RF
review and measured bring-up are separate requirements before an order or vehicle
use. Neither mode claims validated firmware or automotive ratings.

**Note:** the documents in the engineering ZIP (`FABRICATION.md`, `ASSEMBLY.md`,
`DESIGN_NOTES.md`, `BRINGUP.md`) describe **Rev D**. The file sent to the fabricator
is the gerber-only `../pcbway/Racecar-RevD-PCBWAY-GERBERS.zip`, which contains no
documents — order settings must be taken from `../pcbway/PCBWAY-ORDER-GUIDE.md`.

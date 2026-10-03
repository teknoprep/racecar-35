#!/usr/bin/env python3
"""Static consistency checks for crowpanel-arduino/RaceDash/RaceDash.ino.

The dash is one ~12k-line file whose tables are positional and whose redraw
caches must be invalidated by hand; the compiler catches none of that. This
parses the sketch as text (stdlib only) and asserts:

  1. every LastDrawn field is reset in invalidateAll() (or its declaration line
     carries a comment containing "not invalidated" / "intentionally")
  2. NVS key "mon" (sizeof(MonCfg) blob) is in BOTH loadSettings() and
     saveSettings(), and the load is length-guarded
  3. every ST_ enum member (before ST_COUNT) has a ROWS[] entry, and vice versa
  4. PAGE_MON_CFG is in the Page enum, the page draw dispatch and the touch dispatch
  5. the removed LastDrawn fields (temp_x10 ... volt_col_tag) are referenced nowhere
  6. every MonItem has a MON_LABELS entry, a monItemValueX10() case and a
     monRowText() (display format) case
  7. PAGE_MON_ITEM is in the Page enum, the page draw dispatch and the touch dispatch
  8. the "mon" blob is read AND written with sizeof(MonCfg) (the length guard is the
     migration: a resized struct re-seeds from the legacy keys)
  9. monDefaults() seeds thresholds with MON_WARN_OFF and from the legacy s.* fields
 10. the legacy alert rows (retired from the menu) are all still in the SettingId enum -
     they are the migration seed / rollback path and must never be deleted
 11. MON_SRC_NAMES has MON_SRC_COUNT entries, MON_CAN_NAMES has MON_CAN_COUNT entries and
     MON_SRC_MASK has one entry per MonItem (v0.1.156 per-item source)
 12. every item's src[] / can_bus[] is seeded in monDefaults() and range-checked in monCfgValid()
 13. the item page draws a Source row and (for CANBUS) a CAN-bus row, and handles taps on both
 14. the AFR item page HAS an AEM input row (MIR_AEM / "AEM input") that writes s.aem_afr,
     the AFR Source row still drives it (monAfrSyncAem), and Settings hides the AEM row

Usage: tools/validate_dash.py [path/to/RaceDash.ino]   (exit 0 = ok, 1 = failures)
"""
import os
import re
import sys

DEFAULT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..",
                       "crowpanel-arduino", "RaceDash", "RaceDash.ino")

LEGACY_ALERT_ROWS = ["ST_AEM_AFR", "ST_TEMP_WARN_F", "ST_TEMP_WARN_COL", "ST_PSI_WARN_PSI",
                     "ST_PSI_WARN_COL", "ST_VOLT_WARN", "ST_VOLT_WARN_COL", "ST_AFR_WARN_LO",
                     "ST_AFR_WARN_HI", "ST_AFR_WARN_COL", "ST_SHOW_TEMP", "ST_SHOW_PSI",
                     "ST_SHOW_VOLT", "ST_SHOW_AFR"]

REMOVED_FIELDS = ["temp_x10", "temp_col_tag", "psi_x10", "psi_col_tag",
                  "afr_x10", "afr_col_tag", "volt_x10", "volt_col_tag"]


def strip_comments(src):
    """Remove // and /* */ comments, leaving string/char literals alone.
    Newlines are preserved so line numbers still match."""
    out, i, n = [], 0, len(src)
    while i < n:
        c = src[i]
        if c == '"' or c == "'":
            q = c
            out.append(c); i += 1
            while i < n and src[i] != q:
                if src[i] == '\\' and i + 1 < n:
                    out.append(src[i]); i += 1
                out.append(src[i]); i += 1
            if i < n:
                out.append(src[i]); i += 1
        elif src.startswith("//", i):
            while i < n and src[i] != '\n':
                i += 1
        elif src.startswith("/*", i):
            j = src.find("*/", i + 2)
            j = n if j < 0 else j + 2
            out.append("\n" * src.count("\n", i, j))
            i = j
        else:
            out.append(c); i += 1
    return "".join(out)


def block_after(text, header_regex, open_ch="{", close_ch="}"):
    """Return (body, start_line_index) of the brace block following header_regex."""
    m = re.search(header_regex, text)
    if not m:
        return None, -1
    i = text.find(open_ch, m.end() - 1 if text[m.end() - 1] == open_ch else m.end())
    if i < 0:
        return None, -1
    depth, j = 0, i
    while j < len(text):
        if text[j] == open_ch:
            depth += 1
        elif text[j] == close_ch:
            depth -= 1
            if depth == 0:
                return text[i + 1:j], text.count("\n", 0, i)
        j += 1
    return None, -1


def main():
    path = os.path.normpath(sys.argv[1] if len(sys.argv) > 1 else DEFAULT)
    raw = open(path, encoding="utf-8", errors="replace").read()
    code = strip_comments(raw)
    fails = []

    def fail(msg):
        fails.append(msg)

    # ---- 1. LastDrawn vs invalidateAll ------------------------------------
    ld_body_raw, _ = block_after(raw, r"struct\s+LastDrawn\s*\{")
    ld_body, _ = block_after(code, r"struct\s+LastDrawn\s*\{")
    inv_body, _ = block_after(code, r"static\s+void\s+invalidateAll\s*\(\s*\)\s*\{")
    if ld_body is None or inv_body is None:
        fail("1: could not locate struct LastDrawn / invalidateAll()")
    else:
        raw_lines = ld_body_raw.split("\n")
        field_re = re.compile(r"^\s*(?:unsigned\s+)?[A-Za-z_][\w:]*\s+([A-Za-z_]\w*)\s*(?:\[[^\]]*\])?\s*(?:=[^;]*)?;")
        for ln in ld_body.split("\n"):
            m = field_re.match(ln)
            if not m:
                continue
            name = m.group(1)
            if re.search(r"\bld\.%s\b" % re.escape(name), inv_body):
                continue
            # documented exception: a comment on the declaration line
            doc = any(re.search(r"\b%s\b" % re.escape(name), rl) and
                      re.search(r"//.*(not invalidated|intentionally)", rl, re.I)
                      for rl in raw_lines)
            if not doc:
                fail("1: LastDrawn::%s is not reset in invalidateAll() (add it, or comment the "
                     "declaration '// ... intentionally not invalidated')" % name)

    # ---- 2. NVS "mon" blob -------------------------------------------------
    load_body, _ = block_after(code, r"static\s+void\s+loadSettings\s*\(\s*\)\s*\{")
    save_body, _ = block_after(code, r"static\s+void\s+saveSettings\s*\(\s*\)\s*\{")
    if load_body is None or save_body is None:
        fail("2: could not locate loadSettings()/saveSettings()")
    else:
        if not re.search(r'getBytesLength\s*\(\s*"mon"\s*\)\s*==\s*sizeof\(\s*MonCfg\s*\)', load_body):
            fail('2: loadSettings() lacks the getBytesLength("mon") == sizeof(MonCfg) guard')
        if not re.search(r'getBytes\s*\(\s*"mon"\s*,', load_body):
            fail('2: loadSettings() never reads the "mon" blob')
        if not re.search(r'putBytes\s*\(\s*"mon"\s*,[^;]*sizeof\(\s*MonCfg\s*\)', save_body):
            fail('2: saveSettings() lacks prefs.putBytes("mon", &mon_cfg, sizeof(MonCfg))')

    # ---- 3. ST_ enum <-> ROWS[] --------------------------------------------
    enum_body, _ = block_after(code, r"enum\s+SettingId\s*:\s*\w+\s*\{")
    rows_body, _ = block_after(code, r"static\s+const\s+SettingRow\s+ROWS\s*\[[^\]]*\]\s*=\s*\{")
    if enum_body is None or rows_body is None:
        fail("3: could not locate enum SettingId / ROWS[]")
    else:
        members = re.findall(r"\b(ST_[A-Z0-9_]+)\b", enum_body)
        if "ST_COUNT" not in members:
            fail("3: ST_COUNT missing from enum SettingId")
        else:
            members = members[:members.index("ST_COUNT")]   # tail = tool-page action keys, not rows
        row_ids = re.findall(r"\{\s*(ST_[A-Z0-9_]+)\s*,", rows_body)
        for m in members:
            if m not in row_ids:
                fail("3: enum member %s has no ROWS[] entry" % m)
        for r in row_ids:
            if r not in members:
                fail("3: ROWS[] entry %s is not an enum member before ST_COUNT" % r)
        dup = sorted({r for r in row_ids if row_ids.count(r) > 1})
        for r in dup:
            fail("3: ROWS[] lists %s more than once" % r)

    # ---- 4. PAGE_MON_CFG ---------------------------------------------------
    page_body, _ = block_after(code, r"enum\s+Page\s*(?::\s*\w+\s*)?\{")
    if page_body is None or not re.search(r"\bPAGE_MON_CFG\s*=", page_body):
        fail("4: PAGE_MON_CFG missing from the Page enum")
    if not re.search(r"currentPage\s*==\s*PAGE_MON_CFG\s*\)\s*\{[^}]*?\bdrawMonCfg\s*\(\s*\)", code, re.S):
        fail("4: PAGE_MON_CFG has no draw dispatch (drawMonCfg() under currentPage == PAGE_MON_CFG)")
    touch = re.search(r"if\s*\(\s*currentPage\s*==\s*PAGE_MON_CFG\s*\)\s*\{", code)
    touch_ok = False
    for m in re.finditer(r"if\s*\(\s*currentPage\s*==\s*PAGE_MON_CFG\s*\)\s*\{", code):
        if "handleMonCfgTap(" in code[m.end():m.end() + 1200]:
            touch_ok = True
    if not touch_ok:
        fail("4: PAGE_MON_CFG has no touch dispatch (handleMonCfgTap() under currentPage == PAGE_MON_CFG)")

    # ---- 5. removed LastDrawn fields ----------------------------------------
    for f in REMOVED_FIELDS:
        for m in re.finditer(r"\bld\s*\.\s*%s\b" % f, code):
            fail("5: removed field ld.%s still referenced at line %d" % (f, code.count("\n", 0, m.start()) + 1))
        if ld_body and re.search(r"\b%s\b" % f, ld_body):
            fail("5: removed field %s is still declared in LastDrawn" % f)

    # ---- 6. every MonItem has a label + value + format entry ----------------
    mon_body, _ = block_after(code, r"enum\s+MonItem\s*:\s*\w+\s*\{")
    items = re.findall(r"\b(MON_[A-Z0-9_]+)\b", mon_body) if mon_body else []
    if "MON_COUNT" not in items:
        fail("6: could not locate enum MonItem / MON_COUNT")
    else:
        items = items[:items.index("MON_COUNT")]
        lab = re.search(r"MON_LABELS\s*\[[^\]]*\]\s*=\s*\{([^}]*)\}", code)
        nlab = len(re.findall(r'"[^"]*"', lab.group(1))) if lab else -1
        if nlab != len(items):
            fail("6: MON_LABELS has %d entries but enum MonItem has %d items" % (nlab, len(items)))
        val_body, _ = block_after(code, r"static\s+bool\s+monItemValueX10\s*\([^)]*\)\s*\{")
        fmt_body, _ = block_after(code, r"static\s+int32_t\s+monRowText\s*\([^)]*\)\s*\{")
        for it in items:
            if val_body is None or not re.search(r"\bcase\s+%s\b" % it, val_body):
                fail("6: monItemValueX10() has no case for %s" % it)
            if fmt_body is None or not re.search(r"\bcase\s+%s\b" % it, fmt_body):
                fail("6: monRowText() (display format) has no case for %s" % it)

    # ---- 7. PAGE_MON_ITEM ---------------------------------------------------
    if page_body is None or not re.search(r"\bPAGE_MON_ITEM\s*=", page_body):
        fail("7: PAGE_MON_ITEM missing from the Page enum")
    if not re.search(r"currentPage\s*==\s*PAGE_MON_ITEM\s*\)\s*\{[^}]*?\bdrawMonItem\s*\(\s*\)", code, re.S):
        fail("7: PAGE_MON_ITEM has no draw dispatch (drawMonItem() under currentPage == PAGE_MON_ITEM)")
    if not any("handleMonItemTap(" in code[m.end():m.end() + 1200]
               for m in re.finditer(r"if\s*\(\s*currentPage\s*==\s*PAGE_MON_ITEM\s*\)\s*\{", code)):
        fail("7: PAGE_MON_ITEM has no touch dispatch (handleMonItemTap() under currentPage == PAGE_MON_ITEM)")

    # ---- 8. sizeof(MonCfg) on the blob, load AND save ------------------------
    if load_body is not None and not re.search(r'getBytes\s*\(\s*"mon"\s*,[^;]*sizeof\(\s*MonCfg\s*\)', load_body):
        fail('8: loadSettings() must read the "mon" blob with sizeof(MonCfg)')
    if save_body is not None and not re.search(r'putBytes\s*\(\s*"mon"\s*,[^;]*sizeof\(\s*MonCfg\s*\)', save_body):
        fail('8: saveSettings() must write the "mon" blob with sizeof(MonCfg)')

    # ---- 9. monDefaults() uses MON_WARN_OFF + legacy seeds -------------------
    md_body, _ = block_after(code, r"static\s+void\s+monDefaults\s*\(\s*\)\s*\{")
    if md_body is None:
        fail("9: could not locate monDefaults()")
    else:
        if "MON_WARN_OFF" not in md_body:
            fail("9: monDefaults() does not use MON_WARN_OFF")
        for seed in ("coolant_warn_f", "oil_warn_psi", "volt_warn_x10", "afr_warn_lo_x10", "afr_warn_hi_x10"):
            if not re.search(r"\bs\s*\.\s*%s\b" % seed, md_body):
                fail("9: monDefaults() no longer seeds from legacy s.%s" % seed)

    # ---- 10. legacy alert rows must stay in the enum -------------------------
    if enum_body is not None:
        have = set(re.findall(r"\b(ST_[A-Z0-9_]+)\b", enum_body))
        for r in LEGACY_ALERT_ROWS:
            if r not in have:
                fail("10: legacy row %s was deleted from enum SettingId (migration seed / rollback path - keep it, hide it in rowShouldShow())" % r)

    # ---- 11. source / CAN-bus name tables ------------------------------------
    def enum_names(header, count_name):
        body, _ = block_after(code, header)
        mem = re.findall(r"\b([A-Z][A-Z0-9_]+)\b", body) if body else []
        return mem[:mem.index(count_name)] if count_name in mem else None
    srcs = enum_names(r"enum\s+MonSrc\s*:\s*\w+\s*\{", "MON_SRC_COUNT")
    cans = enum_names(r"enum\s+MonCanBus\s*:\s*\w+\s*\{", "MON_CAN_COUNT")
    if srcs is None or cans is None:
        fail("11: could not locate enum MonSrc / MonCanBus (with MON_SRC_COUNT / MON_CAN_COUNT)")
    else:
        for nm, want in (("MON_SRC_NAMES", len(srcs)), ("MON_CAN_NAMES", len(cans))):
            m = re.search(r"%s\s*\[[^\]]*\]\s*=\s*\{([^}]*)\}" % nm, code)
            have = len(re.findall(r'"[^"]*"', m.group(1))) if m else -1
            if have != want:
                fail("11: %s has %d entries but its enum has %d members" % (nm, have, want))
        m = re.search(r"MON_SRC_MASK\s*\[[^\]]*\]\s*=\s*\{(.*?)\};", code, re.S)
        nmask = len(re.findall(r"/\*\s*[A-Z]+\s*\*/", raw[raw.find("MON_SRC_MASK["):][:1500])) if m else -1
        if nmask != len(items):
            fail("11: MON_SRC_MASK has %d entries but enum MonItem has %d items" % (nmask, len(items)))

    # ---- 12. src/can_bus seeded + validated ------------------------------------
    if md_body is not None:
        for fld in ("src", "can_bus"):
            if not re.search(r"mon_cfg\s*\.\s*%s\s*\[[^\]]*\]\s*=" % fld, md_body):
                fail("12: monDefaults() does not seed mon_cfg.%s[]" % fld)
        if not re.search(r"\bs\s*\.\s*sensor_type\b", md_body):
            fail("12: monDefaults() does not seed src[] from the global s.sensor_type")
    mv_body, _ = block_after(code, r"static\s+bool\s+monCfgValid\s*\([^)]*\)\s*\{")
    if mv_body is None:
        fail("12: could not locate monCfgValid()")
    else:
        if not re.search(r"\bsrc\s*\[[^\]]*\]\s*>=\s*MON_SRC_COUNT", mv_body):
            fail("12: monCfgValid() does not reject src >= MON_SRC_COUNT")
        if not re.search(r"\bcan_bus\s*\[[^\]]*\]\s*>=\s*MON_CAN_COUNT", mv_body):
            fail("12: monCfgValid() does not reject can_bus >= MON_CAN_COUNT")

    # ---- 13/14. item page rows ---------------------------------------------------
    mi_body, _ = block_after(code, r"static\s+void\s+drawMonItem\s*\(\s*\)\s*\{")
    rows_fn, _ = block_after(code, r"static\s+int\s+monItemRows\s*\([^)]*\)\s*\{")
    tap_body, _ = block_after(code, r"static\s+void\s+handleMonItemTap\s*\([^)]*\)\s*\{")
    if mi_body is None or rows_fn is None or tap_body is None:
        fail("13: could not locate drawMonItem() / monItemRows() / handleMonItemTap()")
    else:
        if not (re.search(r"\bMIR_SRC\b", rows_fn) and re.search(r'"Source"', mi_body)
                and re.search(r"\bMON_SRC_NAMES\b", mi_body) and re.search(r"\bcase\s+MIR_SRC\b", tap_body)):
            fail("13: the item page does not draw/handle a Source row (MIR_SRC / \"Source\" / MON_SRC_NAMES)")
        if not (re.search(r"\bMON_SRC_CAN\b[^;]*\)?\s*kinds\s*\[[^\]]*\]\s*=\s*MIR_CAN", rows_fn)
                and re.search(r'"CAN bus"', mi_body) and re.search(r"\bMON_CAN_NAMES\b", mi_body)
                and re.search(r"\bcase\s+MIR_CAN\b", tap_body)):
            fail("13: the item page does not draw/handle a CAN-bus row shown only for src == MON_SRC_CAN")
        # v0.1.157: the AEM option is ON the AFR item page as its own "AEM input" row; the
        # AFR Source row still drives s.aem_afr (monAfrSyncAem); the Settings row is gone.
        if not (re.search(r"\bMIR_AEM\b", rows_fn) and re.search(r'"AEM input"', mi_body)
                and re.search(r"\bcase\s+MIR_AEM\b", tap_body)):
            fail("14: the AFR item page has no AEM input row (MIR_AEM / \"AEM input\")")
        if not re.search(r"\bs\.aem_afr\s*=", tap_body):
            fail("14: the AEM input row does not write s.aem_afr")
        if not re.search(r"MON_AFR[^;]*\)\s*monAfrSyncAem|item\s*==\s*MON_AFR\)\s*monAfrSyncAem", tap_body):
            fail("14: the Source row does not drive the AEM input for the AFR item (monAfrSyncAem)")
        if not re.search(r"ST_AEM_STATUS:\s*return false", code):
            fail("14: the AEM row is still visible in Settings (ST_AEM_STATUS must return false)")

    if fails:
        print("validate_dash: FAIL (%d)  %s" % (len(fails), path))
        for f in fails:
            print("  - " + f)
        return 1
    print("validate_dash: OK  %s" % path)
    print("  1 LastDrawn fields all reset in invalidateAll()")
    print('  2 NVS "mon" blob: guarded load + save')
    print("  3 SettingId enum <-> ROWS[] in sync")
    print("  4 PAGE_MON_CFG: enum + draw dispatch + touch dispatch")
    print("  5 no references to removed LastDrawn fields")
    print("  6 every MonItem: label + value case + format case")
    print("  7 PAGE_MON_ITEM: enum + draw dispatch + touch dispatch")
    print('  8 "mon" blob sized with sizeof(MonCfg) in load + save')
    print("  9 monDefaults(): MON_WARN_OFF + legacy seeds")
    print(" 10 legacy alert rows still in enum SettingId")
    print(" 11 MON_SRC_NAMES / MON_CAN_NAMES / MON_SRC_MASK lengths match their enums")
    print(" 12 per-item src + can_bus seeded in monDefaults(), validated in monCfgValid()")
    print(" 13 item page: Source row + CAN-bus row (CANBUS only), draw + tap")
    print(" 14 AFR has an AEM input row (writes s.aem_afr); Source drives it; hidden in Settings")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Sessions-list column sorting (server/app/main.py).

The list is the pit wall's front door, and an admin sees EVERY user's sessions
in it — so "newest first, across users" and "click any header to sort" are the
contract, not decoration. Two layers are pinned here:

  * the markup the server renders: a sortable header per column plus
    machine-sortable VALUES, because a formatted date ("2026-10-07 08:28:27
    UTC") and a human size ("1.5 MB") do not sort as text;
  * the sorter itself, executed under a tiny DOM stub so the real code runs:
    direction toggle, numeric vs text, missing values last, stable order.

No fastapi needed (same approach as tests/test_server_version.py). The stub
implements only the handful of DOM calls the sorter uses; a REAL browser pass
(Playwright against a live uvicorn) is what verified it beyond that.
"""
import ast
import pathlib
import re
import shutil
import subprocess
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
MAIN = ROOT / "server/app/main.py"
SRC = MAIN.read_text()

COLUMNS = ("user", "started", "track", "best", "filename", "size")


def _evaluated(name):
    """The literal string assigned to `name` in main.py."""
    for node in ast.parse(SRC).body:
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id == name:
                    return ast.literal_eval(node.value)
    raise AssertionError(f"{name} not found in main.py")


def _sort_js():
    """The sorter exactly as shipped, minus the rest of the page script."""
    js = _evaluated("_INDEX_JS")
    start = js.index("// ---- column sorting")
    end = js.index("// ---- best lap per session")
    return js[start:end]


_DOM_STUB = r"""
// ---- minimal DOM: only what the sorter touches -------------------------
function El(tag, opts){
  opts = opts || {};
  this.tag = tag; this.children = []; this.parent = null;
  this.dataset = opts.dataset || {};
  this.textContent = opts.text || '';
  this._cl = new Set(String(opts.classes || '').split(' ').filter(Boolean));
  var cl = this._cl;
  this.classList = { toggle: function(c, on){ if (on) cl.add(c); else cl.delete(c); },
                     contains: function(c){ return cl.has(c); } };
}
El.prototype.appendChild = function(k){
  if (k.parent) k.parent.children = k.parent.children.filter(function(x){ return x !== k; });
  k.parent = this; this.children.push(k); return k;
};
El.prototype.addEventListener = function(ev, fn){ this._ev = this._ev || {}; this._ev[ev] = fn; };
El.prototype.click = function(){ if (this._ev && this._ev.click) this._ev.click(); };
El.prototype.querySelector = function(sel){ return this.querySelectorAll(sel)[0] || null; };
El.prototype.querySelectorAll = function(sel){
  var out = [], self = this;
  (function walk(n){ n.children.forEach(function(k){ if (match(k, sel)) out.push(k); walk(k); }); })(self);
  return out;
};
function match(el, sel){
  if (sel === 'tr') return el.tag === 'tr';
  if (sel === '.ind') return el._cl.has('ind');
  if (sel === 'th.sortable') return el.tag === 'th' && el._cl.has('sortable');
  var m = /^td\[data-k="([^"]+)"\]$/.exec(sel);
  if (m) return el.tag === 'td' && el.dataset.k === m[1];
  return false;
}
function td(k, v, text){
  var e = new El('td', {dataset: {k: k}, text: text === undefined ? String(v) : text});
  if (v !== undefined && v !== null) e.dataset.sort = String(v);
  return e;
}
function row(cells){ var tr = new El('tr'); cells.forEach(function(c){ tr.appendChild(c); }); return tr; }

// ---- the fixture: what the server renders (newest first) -----------------
// alice@x and bob@x hold ONE mixed time list; two sessions have no best lap.
var DATA = [
  {user: 'bob',  started: 1780000500, track: 'Watkins',   best: '',   size: 8000, file: 'b_500.ndjson'},
  {user: 'alice',started: 1780000400, track: 'Shenandoah',best: '49.0', size: 90, file: 'a_400.ndjson'},
  {user: 'bob',  started: 1780000300, track: 'Jefferson', best: '45.0', size: 900, file: 'b_300.ndjson'},
  {user: 'alice',started: 1780000200, track: 'Thompson',  best: '48.0', size: 4000, file: 'a_200.ndjson'},
  {user: 'bob',  started: 1780000100, track: 'Summit',    best: '',   size: 90, file: 'b_100.ndjson'},
];
var tbody = new El('tbody');
DATA.forEach(function(d){
  tbody.appendChild(row([
    new El('td'),                                  // checkbox column (not sortable)
    td('user', d.user),
    td('started', d.started),
    td('track', d.track),
    td('best', d.best, d.best === '' ? '\u2014' : d.best),
    td('filename', d.file),
    td('size', d.size, d.size + ' B'),
    new El('td'),                                  // actions column
  ]));
});
var heads = ['user','started','track','best','filename','size'].map(function(k){
  var th = new El('th', {classes: 'sortable', dataset: {key: k}});
  th.appendChild(new El('span', {classes: 'ind'}));
  return th;
});
global.document = {
  getElementById: function(id){ return id === 'rows' ? tbody : null; },
  querySelectorAll: function(sel){ return sel === 'th.sortable' ? heads : []; },
};
function head(k){ return heads.filter(function(h){ return h.dataset.key === k; })[0]; }
function ind(k){ return head(k).querySelector('.ind').textContent; }
function col(k){
  return tbody.querySelectorAll('tr').map(function(tr){
    var c = tr.querySelector('td[data-k="' + k + '"]');
    return c && c.dataset.sort !== undefined ? c.dataset.sort : (c ? c.textContent : '');
  });
}
function fileOrder(){ return col('filename'); }
function fields(){ return tbody.querySelectorAll('tr').map(function(tr){
  return tr.querySelector('td[data-k="filename"]').textContent; }); }
var FAILS = [];
function ok(cond, msg){ console.log((cond ? '  ok   ' : '  FAIL ') + msg); if (!cond) FAILS.push(msg); }
function eq(got, want, msg){ ok(JSON.stringify(got) === JSON.stringify(want),
  msg + '  got=' + JSON.stringify(got)); }
"""

_CHECKS = r"""
// ---- the checks ----------------------------------------------------------
eq(fileOrder(), ['b_500.ndjson','a_400.ndjson','b_300.ndjson','a_200.ndjson','b_100.ndjson'],
   'renders newest-first across users (apply() keeps the server order)');
ok(ind('started') === '\u25be', 'started opens marked descending');
ok(heads.filter(function(h){ return h.querySelector('.ind').textContent; }).length === 1,
   'only the active column carries an arrow');

head('started').click();
eq(col('started').map(Number).sort(function(a,b){ return a-b; }), col('started').map(Number),
   'click on started -> ascending');
ok(ind('started') === '\u25b4', 'arrow flips to ascending');
head('started').click();
eq(col('started').map(Number), [1780000500,1780000400,1780000300,1780000200,1780000100],
   'click again -> descending');

head('size').click();
eq(col('size').map(Number), [8000,4000,900,90,90], 'size opens descending, NUMERIC (not text)');
head('size').click();
eq(col('size').map(Number), [90,90,8000,4000,900].sort(function(a,b){ return a-b; }), 'size reverses');
eq(fileOrder(), ['a_400.ndjson','b_100.ndjson','b_300.ndjson','a_200.ndjson','b_500.ndjson'],
   'equal sizes keep their previous relative order (stable)');

head('user').click();
eq(col('user'), ['alice','alice','bob','bob','bob'], 'user sorts A-Z first');
head('user').click();
eq(col('user'), ['bob','bob','bob','alice','alice'], 'user reverses to Z-A');

head('track').click();
var tr = col('track');
ok(tr.slice().sort().join() === tr.join(), 'track sorts A-Z');
head('filename').click();
eq(fileOrder(), ['a_200.ndjson','a_400.ndjson','b_100.ndjson','b_300.ndjson','b_500.ndjson'],
   'filename sorts A-Z');

head('best').click();
ok(ind('best') === '\u25be', 'best lap opens descending (a lap time, not a label)');
var bl = col('best');
eq(bl.slice(0, 3), ['49.0','48.0','45.0'], 'best lap descending');
eq(bl.slice(3), ['',''], 'un-timed sessions last (descending)');
head('best').click();
bl = col('best');
eq(bl.slice(0, 3), ['45.0','48.0','49.0'], 'best lap ascending');
eq(bl.slice(3), ['',''], 'un-timed sessions STILL last (ascending)');

if (FAILS.length){ console.log('\n' + FAILS.length + ' FAILED'); process.exit(1); }
console.log('\nALL SORTER CHECKS PASSED');
"""


class TestIndexSortMarkup(unittest.TestCase):
    def test_every_column_header_is_sortable(self):
        for k in COLUMNS:
            self.assertIn(f'data-key="{k}"', SRC, f"{k} header is not sortable")
        self.assertEqual(len(re.findall(r'class="sortable(?: num)?" data-key="', SRC)),
                         len(COLUMNS), "a sortable header is missing (or one is extra)")
        self.assertEqual(SRC.count('class="ind"'), len(COLUMNS),
                         "each sortable header needs exactly one indicator span")
        # the checkbox + actions columns stay unsorted (no data-key)
        self.assertIn('"<table><thead><tr><th></th>', SRC)

    def test_headers_do_not_wrap_or_overlap(self):
        self.assertIn("th.sortable {", SRC.replace("{{", "{").replace("}}", "}"))
        self.assertIn("white-space: nowrap", SRC)

    def test_rows_carry_machine_sortable_values(self):
        for k in COLUMNS:
            self.assertIn(f'data-k="{k}"', SRC, f"row cell for {k} missing")
        # time and size must NOT sort on their rendered text
        self.assertIn('data-k="started" data-sort="{epoch}"', SRC)
        self.assertIn('data-k="size" data-sort="{st.st_size}"', SRC)
        self.assertIn('data-k="best" data-best="{user_h}/{file_h}"', SRC)
        self.assertIn("c.dataset.sort = (d.best_s == null ? '' : d.best_s)", SRC,
                      "the async best-lap fill must feed the sorter")

    def test_rows_render_as_one_time_list_across_users(self):
        """Rows are collected per user dir, then re-ordered — an admin sees
        newest-first across everyone, not 'all of alice, then all of bob'."""
        self.assertIn("rows: list[tuple[int, str]]", SRC)
        self.assertIn('sorted(rows, key=lambda t: t[0], reverse=True)', SRC)
        # and the client agrees about the opening order
        self.assertIn("let key = 'started', dir = -1;", SRC)

    def test_missing_values_sort_last_in_both_directions(self):
        self.assertIn("if (ma !== mb) return ma ? 1 : -1;", SRC)

    def test_old_best_lap_only_sorter_is_gone(self):
        """One sorter, or a click on 'best' fights the generic one."""
        self.assertNotIn("th.best", SRC)
        self.assertNotIn("dataset.secs", SRC)

    @unittest.skipUnless(shutil.which("node"), "node not installed")
    def test_page_scripts_parse(self):
        """_INDEX_JS is a NON-raw Python string: a JS '\\n' written with one
        backslash becomes a real newline inside a string literal and the
        script dies (which is exactly what sorting is made of)."""
        html = _evaluated("_INDEX_JS")
        blocks = re.findall(r"<script>(.*?)</script>", html, re.S)
        self.assertGreaterEqual(len(blocks), 1)
        for i, js in enumerate(blocks):
            with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as f:
                f.write(js)
            try:
                r = subprocess.run([shutil.which("node"), "--check", f.name],
                                   capture_output=True, text=True)
            finally:
                pathlib.Path(f.name).unlink(missing_ok=True)
            self.assertEqual(r.returncode, 0, f"index <script> #{i}: {r.stderr[:400]}")

    @unittest.skipUnless(shutil.which("node"), "node not installed")
    def test_sorter_logic_runs_against_the_fixture(self):
        js = _DOM_STUB + "\n" + _sort_js() + "\n" + _CHECKS
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as f:
            f.write(js)
        try:
            r = subprocess.run([shutil.which("node"), f.name], capture_output=True, text=True)
        finally:
            pathlib.Path(f.name).unlink(missing_ok=True)
        if r.returncode != 0:
            self.fail("sorter behaviour check failed:\n" + r.stdout + r.stderr[:1200])
        self.assertIn("ALL SORTER CHECKS PASSED", r.stdout)


if __name__ == "__main__":
    unittest.main()

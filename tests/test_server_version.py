"""Which server build is running (server/app/main.py: server_version()).

The admin page shows this next to its "update server" button, and GET /version
exposes it, because the server has no semver of its own — the identity IS the
commit the image was built from. It has two sources (`RACECAR_BUILD_*` stamped
into the image at build time, else the host checkout bind-mounted at /repo) and
the whole point is which one wins and when: getting that backwards reports the
checkout of a server that was pulled-but-never-rebuilt as if it were running.

The helpers are pure os/pathlib/re/zlib, so they are evaluated out of the module
and exercised against real .git layouts — no fastapi needed (the host has none).
"""
import ast
import os
import pathlib
import re
import shutil
import subprocess
import tempfile
import time
import unittest
import zlib

ROOT = pathlib.Path(__file__).resolve().parents[1]
MAIN = ROOT / "server/app/main.py"
SRC = MAIN.read_text()

_WANTED_VARS = ("BUILD_SHA", "BUILD_SUBJECT", "BUILD_TIME", "REPO_MOUNT")
_WANTED_FUNCS = ("_repo_dotgit", "_git_head", "_git_subject", "server_version")


def _sandbox(env, sha_in_image="", subject_in_image="", mount=None):
    """The real constants + functions from main.py, executed with a controlled
    environment (BUILD_* are read from os.environ at exec time)."""
    tree = ast.parse(SRC)
    parts = []
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id in _WANTED_VARS for t in node.targets):
            parts.append(ast.get_source_segment(SRC, node))
        elif isinstance(node, ast.FunctionDef) and node.name in _WANTED_FUNCS:
            parts.append(ast.get_source_segment(SRC, node))
    assert len(parts) == len(_WANTED_VARS) + len(_WANTED_FUNCS), \
        "main.py's version helpers changed shape — update this test"

    keep = {k: os.environ.get(k) for k in
            ("RACECAR_BUILD_SHA", "RACECAR_BUILD_SUBJECT",
             "RACECAR_BUILD_TIME", "RACECAR_REPO_MOUNT")}
    try:
        for k, v in keep.items():
            os.environ.pop(k, None)
        if sha_in_image:
            os.environ["RACECAR_BUILD_SHA"] = sha_in_image
            os.environ["RACECAR_BUILD_SUBJECT"] = subject_in_image
            os.environ["RACECAR_BUILD_TIME"] = "2026-10-05T00:00:00Z"
        os.environ["RACECAR_REPO_MOUNT"] = str(mount) if mount else "/nonexistent-repo"
        ns = {"os": os, "pathlib": pathlib, "re": re, "zlib": zlib,
              "time": time, "_PROC_START": 1_000}
        exec("\n\n".join(parts), ns)
        return ns
    finally:
        for k, v in keep.items():
            os.environ.pop(k, None)
            if v is not None:
                os.environ[k] = v


def _evaluated(name):
    """A module-level HTML constant, evaluated out of main.py (string
    concatenation of literals/names/placeholder-free f-strings)."""
    tree = ast.parse(SRC)

    def ev(node, ns):
        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            return ev(node.left, ns) + ev(node.right, ns)
        if isinstance(node, ast.Name):
            return ns[node.id]
        if isinstance(node, ast.JoinedStr):
            return "".join(ev(v, ns) for v in node.values)
        if isinstance(node, ast.FormattedValue):
            return "X"
        raise ValueError(ast.dump(node))

    ns = {}
    for _ in range(8):
        for n in tree.body:
            if (isinstance(n, ast.Assign) and len(n.targets) == 1
                    and isinstance(n.targets[0], ast.Name)):
                try:
                    ns[n.targets[0].id] = ev(n.value, ns)
                except Exception:
                    pass
    return ns[name]


def _sha(ch):
    """A valid-looking 40-char sha (hex only — an invalid 'sha' is a different
    test, and would silently exercise the rejection path instead)."""
    assert len(ch) == 1 and ch in "0123456789abcdef", ch
    return ch * 40


def _commit_object(repo, sha, subject, body="more\n"):
    """Write a loose commit object exactly as git would (zlib, `<type> <len>\\0`)."""
    content = (f"tree {_sha('b')}\n"
               f"author a <a@b> 1789000000 +0000\n"
               f"committer a <a@b> 1789000000 +0000\n\n"
               f"{subject}\n\n{body}").encode()
    obj = b"commit " + str(len(content)).encode() + b"\x00" + content
    p = repo / ".git/objects" / sha[:2] / sha[2:]
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(zlib.compress(obj))


def _repo(tmp, sha=_sha("a"), subject="Subject line", branch="main"):
    repo = pathlib.Path(tmp)
    (repo / ".git/refs/heads").mkdir(parents=True, exist_ok=True)
    (repo / ".git/HEAD").write_text(f"ref: refs/heads/{branch}\n")
    (repo / f".git/refs/heads/{branch}").write_text(sha + "\n")
    _commit_object(repo, sha, subject)
    return repo


class TestServerVersion(unittest.TestCase):
    def test_checkout_is_the_fallback_and_carries_its_subject(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            repo = _repo(tmp, sha=_sha("a"), subject="3D view v12")
            v = _sandbox({}, mount=repo)["server_version"]()
        self.assertEqual(v["source"], "repo")
        self.assertEqual(v["sha"], _sha("a"))
        self.assertEqual(v["short"], "a" * 7)
        self.assertEqual(v["subject"], "3D view v12")
        self.assertEqual(v["display"], "aaaaaaa \u00b7 3D view v12")
        # the checkout IS what we fell back to, so nothing is pending
        self.assertFalse(v["deploy_pending"])
        self.assertEqual(v["repo_sha"], _sha("a"))

    def test_image_stamp_wins_and_flags_a_newer_checkout(self):
        """The failure this prevents: someone pulls without rebuilding, and the
        page claims the server is already on the new code."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            repo = _repo(tmp, sha=_sha("b"), subject="newer commit")
            v = _sandbox({}, sha_in_image=_sha("a"),
                         subject_in_image="stamped at build",
                         mount=repo)["server_version"]()
        self.assertEqual(v["source"], "image")
        self.assertEqual(v["sha"], _sha("a"))
        self.assertEqual(v["display"], "aaaaaaa \u00b7 stamped at build")
        self.assertEqual(v["built"], "2026-10-05T00:00:00Z")
        self.assertEqual(v["repo_sha"], _sha("b"))
        self.assertEqual(v["repo_short"], "b" * 7)
        self.assertTrue(v["deploy_pending"])

    def test_matching_checkout_is_not_pending(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            repo = _repo(tmp, sha=_sha("c"))
            v = _sandbox({}, sha_in_image=_sha("c"), subject_in_image="s",
                         mount=repo)["server_version"]()
        self.assertEqual(v["source"], "image")
        self.assertFalse(v["deploy_pending"])

    def test_long_subject_is_trimmed_for_the_header_only(self):
        import tempfile
        subj = "Release v0.1.171: threshold keypad ('.'+OFF); AEM folds into the AFR Source menu"
        with tempfile.TemporaryDirectory() as tmp:
            repo = _repo(tmp, sha=_sha("a"), subject=subj)
            v = _sandbox({}, mount=repo)["server_version"]()
        self.assertEqual(v["subject"], subj)                 # full, for the tooltip
        self.assertTrue(v["display"].endswith("\u2026"))
        self.assertLessEqual(len(v["display"]), 7 + 3 + 48)

    def test_nothing_to_report_is_not_an_error(self):
        v = _sandbox({}, mount=None)["server_version"]()
        self.assertEqual(v["sha"], "")
        self.assertEqual(v["display"], "unknown")
        self.assertFalse(v["deploy_pending"])
        self.assertGreaterEqual(v["uptime_s"], 0)

    def test_packed_refs(self):
        """After a gc/fetch the branch ref lives in .git/packed-refs."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            repo = pathlib.Path(tmp)
            (repo / ".git").mkdir(parents=True)
            sha = _sha("d")
            (repo / ".git/HEAD").write_text("ref: refs/heads/main\n")
            (repo / ".git/packed-refs").write_text(
                "# pack-refs with: peeled fully-peeled sorted\n"
                f"{sha} refs/heads/main\n^{_sha('e')}\n")
            v = _sandbox({}, mount=repo)["server_version"]()
        self.assertEqual(v["sha"], sha)
        self.assertEqual(v["short"], "d" * 7)

    def test_detached_head(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            repo = pathlib.Path(tmp)
            (repo / ".git").mkdir(parents=True)
            (repo / ".git/HEAD").write_text(_sha("f") + "\n")
            v = _sandbox({}, mount=repo)["server_version"]()
        self.assertEqual(v["sha"], _sha("f"))

    def test_garbage_head_never_raises(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            repo = pathlib.Path(tmp)
            (repo / ".git").mkdir(parents=True)
            (repo / ".git/HEAD").write_text("not a ref, not a sha\n")
            v = _sandbox({}, mount=repo)["server_version"]()
        self.assertEqual(v["sha"], "")
        self.assertEqual(v["display"], "unknown")

    def test_packed_object_still_reports_the_sha(self):
        """A gc'd (packed) commit has no loose object to decompress: the subject
        is unavailable, but the sha must still be reported."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            repo = pathlib.Path(tmp)
            (repo / ".git/refs/heads").mkdir(parents=True)
            (repo / ".git/HEAD").write_text("ref: refs/heads/main\n")
            (repo / ".git/refs/heads/main").write_text(_sha("9") + "\n")
            v = _sandbox({}, mount=repo)["server_version"]()
        self.assertEqual(v["short"], "9" * 7)
        self.assertEqual(v["subject"], "")
        self.assertEqual(v["display"], "9" * 7)


class TestWiring(unittest.TestCase):
    """The identity is only useful if it reaches the page — and if the build
    actually stamps it. These are the wires a refactor could pull out."""

    def test_version_endpoint_exists_and_is_public(self):
        self.assertIn('@app.get("/version")', SRC)
        block = SRC[SRC.index('@app.get("/version")'):]
        block = block[:block.index("\n@app.")]
        self.assertIn("server_version()", block)
        self.assertNotIn("require_admin", block)
        self.assertNotIn("require_web_user", block)

    def test_admin_status_and_caps_report_it(self):
        self.assertIn('"version": server_version(),', SRC)
        caps = SRC[SRC.index('@app.get("/caps")'):]
        caps = caps[:caps.index("\n@app.")]
        for key in ('"server"', '"server_sha"', '"server_source"', '"deploy_pending"'):
            self.assertIn(key, caps, key)

    def test_admin_page_shows_the_version_when_idle(self):
        admin = SRC[SRC.index("_ADMIN_HTML = ("):]
        admin = admin[:admin.index("_ADMIN_DISABLED_HTML")]
        self.assertIn("function ver(j)", admin)
        self.assertIn("j.version", admin)
        self.assertIn("m.textContent='v '+ver(j)", admin)      # after the restart
        self.assertIn("m.textContent = 'v ' + ver(j)", admin)  # idle label
        self.assertIn("deploy_pending", admin)

    @unittest.skipUnless(shutil.which("node"), "node not installed")
    def test_admin_page_scripts_parse(self):
        """The admin HTML is a NON-raw Python string: a JS '\\n' written with one
        backslash becomes a real newline inside a JS string literal and the
        whole header script dies (button, version label, everything). That
        exact bug was written once while adding the version label."""
        html = _evaluated("_ADMIN_HTML")
        blocks = re.findall(r"<script>(.*?)</script>", html, re.S)
        self.assertGreaterEqual(len(blocks), 2)
        for i, js in enumerate(blocks):
            with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as f:
                f.write(js)
            try:
                r = subprocess.run([shutil.which("node"), "--check", f.name],
                                   capture_output=True, text=True)
            finally:
                os.unlink(f.name)
            self.assertEqual(r.returncode, 0, f"admin <script> #{i}: {r.stderr[:400]}")

    def test_build_stamp_is_wired_through_docker_and_the_updater(self):
        dockerfile = (ROOT / "server/Dockerfile").read_text()
        self.assertIn("ARG GIT_SHA", dockerfile)
        self.assertIn("RACECAR_BUILD_SHA=$GIT_SHA", dockerfile)
        for name in ("docker-compose.yml", "docker-compose.prod.yml"):
            comp = (ROOT / "server" / name).read_text()
            self.assertIn('GIT_SHA: "${RACECAR_BUILD_SHA:-}"', comp)
            # the fallback mount: the app parses .git when there is no stamp
            self.assertIn("../.git:/repo/.git:ro", comp)
        upd = (ROOT / "server/host_updater.sh").read_text()
        self.assertIn('export RACECAR_BUILD_SHA="$sha"', upd)
        self.assertIn("git -C \"$REPO\" rev-parse --short HEAD", upd)


if __name__ == "__main__":
    unittest.main()

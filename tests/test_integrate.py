import subprocess
from fastws._toml import tomllib
from fastws.integrate import _config, code_lines, owners, snapshot


def git(repo, *args): return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True).stdout


def test_snapshot_takes_the_working_tree_and_leaves_head_and_index(tmp_path):
    git(tmp_path, "init", "-q")
    (tmp_path/".gitignore").write_text("ignored\n")
    (tmp_path/"kept").write_text("old")
    git(tmp_path, "add", "-A"); git(tmp_path, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "base")
    head = git(tmp_path, "rev-parse", "HEAD")
    (tmp_path/"kept").write_text("new"); (tmp_path/"untracked").write_text("u"); (tmp_path/"ignored").write_text("i")
    commit = snapshot(tmp_path)
    assert git(tmp_path, "show", f"{commit}:kept") == "new"
    assert git(tmp_path, "show", f"{commit}:untracked") == "u"
    assert "ignored" not in git(tmp_path, "ls-tree", "--name-only", commit).split()
    assert git(tmp_path, "rev-parse", "HEAD") == head and git(tmp_path, "diff", "--cached", "--name-only") == ""


def test_config_points_only_the_roots_crates_at_the_root(tmp_path):
    workspace, root = tmp_path/"ws", tmp_path/"root"
    (workspace/".cargo").mkdir(parents=True)
    (workspace/".cargo"/"config.toml").write_text(
        f'[build]\nrustc-wrapper = "/lock"\n\n[patch.crates-io]\na = {{ path = "{workspace}/a" }}\nb = {{ path = "{workspace}/b" }}\n')
    (root/"a").mkdir(parents=True)
    (root/"a"/"Cargo.toml").write_text('[package]\nname = "a"\nversion = "0.1.0"\n')
    (root/"c").mkdir()
    (root/"c"/"Cargo.toml").write_text('[package]\nname = "c"\nversion = "0.1.0"\n\n[dependencies]\na = "0.1"\n')
    _config(root, workspace)
    config = tomllib.loads((root/".cargo"/"config.toml").read_text())
    assert config["build"]["rustc-wrapper"] == "/lock"
    assert config["patch"]["crates-io"] == {"a": {"path": str(root/"a")}, "b": {"path": f"{workspace}/b"}}


def test_code_lines_skips_blanks_and_comments(tmp_path):
    (tmp_path/"a").mkdir()
    (tmp_path/"a"/"m.rs").write_text("//! Module doc\n\nfn f() {}\n/* a\n   block */\nlet s = \"// not a comment\";\n  /// doc\n}\n")
    (tmp_path/"notes.md").write_text("fn f() {}\n")
    assert code_lines(tmp_path) == 3


def test_owners_reads_the_files_section_in_order(tmp_path):
    (tmp_path/"owners.md").write_text(
        "# Owners\n\n- Before: `x/*`\n\n## Files\n\nThe first match owns a path.\n- Structure: `a/src/p.rs` `b/src/s.rs`\n- Numeric: `b/*`\n\n## Order\n\n- After: `c/*`\n")
    assert list(owners(tmp_path/"owners.md").items()) == [("Structure", ["a/src/p.rs", "b/src/s.rs"]), ("Numeric", ["b/*"])]

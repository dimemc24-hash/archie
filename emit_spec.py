#!/usr/bin/env python3
"""
Stage-1 spec-bundle emitter for the dev-orchestration harness. Turns ideation output into a
pushed `harness/spec/<id>` branch that Stage 2 (stage2_build.py) consumes.

MUST run on /profile dev (LOCAL terminal): it needs the GitHub deploy key (~/.ssh/github_do)
and the target repo checkout, which the sandboxed `default` profile cannot reach.

Usage:
  emit_spec.py --run-id YYYY-MM-DD-slug \
               --spec build-spec.md --prompt build-prompt.md --manifest checkpoint-manifest.json \
               [--kb KB_DIR] [--push] [--profile NAME]

--profile selects the target repo (profiles/<name>.json); omit it for the legacy default, the
NewChapter checkout at ~/harness/repo.

Guards (refuses to emit a bad bundle — same gates Stage 2 enforces):
  * run-id shape YYYY-MM-DD-slug
  * checkpoint-manifest.json has a 'checkpoints' array; each checkpoint has id/trigger/question/on_synthesis; ids unique
  * build-prompt.md embeds the sentinel protocol (CHECKPOINT_REACHED + BUILD_COMPLETE)
Writes _harness/<id>/{build-spec.md,build-prompt.md,checkpoint-manifest.json,kb/} on a fresh
harness/spec/<id> branch off origin/main, commits, and (with --push) pushes to origin.
"""
import argparse, json, os, re, subprocess, sys, shutil

HARNESS = os.path.expanduser("~/harness")
PROFILES_DIR = os.path.join(HARNESS, "profiles")
REPO = os.path.join(HARNESS, "repo")  # default (legacy/no --profile): the NewChapter checkout
REQ = {"id", "trigger", "question", "on_synthesis"}

def run(cmd, cwd=None):
    # cwd resolved at CALL time so a --profile reassigning the global REPO earlier is honored —
    # a bare `cwd=REPO` default would freeze the module-load-time value (see stage2_build.py).
    p = subprocess.run(cmd, cwd=cwd if cwd is not None else REPO, capture_output=True, text=True)
    return p.returncode, p.stdout.strip(), p.stderr.strip()

def resolve_repo(profile):
    """Resolve the target repo checkout for this run. See stage2_build.py's resolve_repo — same
    contract, duplicated here since these are two independently-invoked scripts."""
    if not profile:
        return os.path.join(HARNESS, "repo")
    cfg_path = os.path.join(PROFILES_DIR, f"{profile}.json")
    try:
        cfg = json.load(open(cfg_path))
    except Exception as e:
        die(f"unknown profile '{profile}': cannot read {cfg_path}: {e}")
    workspace = os.path.expanduser(cfg["workspace"])
    if not os.path.isdir(os.path.join(workspace, ".git")):
        os.makedirs(os.path.dirname(workspace), exist_ok=True)
        rc, _, err = run(["git", "clone", cfg["repo_url"], workspace], HARNESS)
        if rc:
            die(f"failed to clone {cfg['repo_url']} into {workspace}: {err}")
    return workspace

def die(m):
    print("ERROR:", m); sys.exit(1)

def main():
    global REPO
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--spec", required=True)
    ap.add_argument("--prompt", required=True)
    ap.add_argument("--manifest", required=True)
    ap.add_argument("--kb")
    ap.add_argument("--push", action="store_true")
    ap.add_argument("--profile", default=None,
                     help="target repo profile (profiles/<name>.json); omit for the legacy NewChapter checkout")
    a = ap.parse_args()

    REPO = resolve_repo(a.profile)  # must happen BEFORE any run()/git call below

    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}-[a-z0-9][a-z0-9-]*", a.run_id):
        die(f"run-id must look like YYYY-MM-DD-slug (got {a.run_id!r})")
    for f in (a.spec, a.prompt, a.manifest):
        if not os.path.isfile(f):
            die(f"missing file: {f}")

    try:
        m = json.load(open(a.manifest))
    except Exception as e:
        die(f"manifest is not valid JSON: {e}")
    if not isinstance(m, dict) or not isinstance(m.get("checkpoints"), list):
        die("manifest needs a 'checkpoints' array")
    seen = set()
    for cp in m["checkpoints"]:
        miss = REQ - set(cp or {})
        if miss:
            die(f"checkpoint missing keys {sorted(miss)}")
        if cp["id"] in seen:
            die(f"duplicate checkpoint id {cp['id']}")
        seen.add(cp["id"])

    bp = open(a.prompt).read()
    if "CHECKPOINT_REACHED" not in bp or "BUILD_COMPLETE" not in bp:
        die("build-prompt.md must embed the sentinel protocol (CHECKPOINT_REACHED:<id> + BUILD_COMPLETE)")

    branch = f"harness/spec/{a.run_id}"
    rc, _, e = run(["git", "fetch", "origin", "main"]);  rc and die(f"git fetch failed: {e}")
    rc, _, e = run(["git", "checkout", "-B", branch, "origin/main"]); rc and die(f"checkout failed: {e}")

    d = os.path.join(REPO, "_harness", a.run_id)
    os.makedirs(os.path.join(d, "kb"), exist_ok=True)
    shutil.copy(a.spec, os.path.join(d, "build-spec.md"))
    shutil.copy(a.prompt, os.path.join(d, "build-prompt.md"))
    json.dump(m, open(os.path.join(d, "checkpoint-manifest.json"), "w"), indent=2)
    if a.kb and os.path.isdir(a.kb):
        for fn in os.listdir(a.kb):
            src = os.path.join(a.kb, fn)
            if os.path.isfile(src):
                shutil.copy(src, os.path.join(d, "kb", fn))

    run(["git", "add", f"_harness/{a.run_id}"])
    rc, _, e = run(["git", "-c", "user.name=stage1-archie", "-c", "user.email=stage1@fsfai",
                    "commit", "-qm", f"stage1: spec bundle ({a.run_id})"])
    if rc:
        run(["git", "checkout", "main"]); die(f"commit failed (nothing staged?): {e}")

    if a.push:
        rc, _, e = run(["git", "push", "-u", "origin", branch])
        run(["git", "checkout", "main"])
        rc and die(f"push failed: {e}")
        print(f"PUSHED {branch}")
    else:
        run(["git", "checkout", "main"])
        print(f"COMMITTED {branch} locally (not pushed; rerun with --push)")
    print(f"checkpoints={len(m['checkpoints'])}. Kick the build with:  stage2_build.py --run-id {a.run_id}")

if __name__ == "__main__":
    main()

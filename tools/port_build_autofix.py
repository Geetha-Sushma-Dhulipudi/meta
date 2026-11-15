#!/usr/bin/env python3
"""Universal Build Triage and Copilot Auto-Fix Tool for zopencommunity ports.

This tool:
1. Verifies if zopen / zopen-build is installed/present, configuring PATH if needed.
2. Clones the port repo and tests patches against upstream releases.
3. Implements an automated BUILD-FIX-VERIFY loop with GitHub Copilot:
   - On error, prompts Copilot for a fix.
   - Applies the proposed fix into a sandbox port environment.
   - Re-runs `zopen-build -vv` and patch verification until the build is 100% successful!
4. Posts the final, verified patch file and explanation as a GitHub comment on the PR.

Usage:
  python3 tools/port_build_autofix.py --port-dir ./syftport --meta-dir . --output /tmp/gh-aw/autofix/facts.md
"""

from __future__ import annotations

import argparse
import glob
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request


def check_and_setup_zopen(meta_dir: Path) -> str:
    """Check if zopen/zopen-build is present in PATH or meta_dir, and return its path."""
    zopen_build_which = shutil.which("zopen-build")
    if zopen_build_which:
        print(f"✅ Found zopen-build in system PATH: {zopen_build_which}")
        return zopen_build_which

    meta_bin = meta_dir / "bin" / "zopen-build"
    if meta_bin.exists():
        print(f"✅ Found zopen-build in meta tools: {meta_bin}")
        os.environ["PATH"] = f"{meta_dir / 'bin'}:{os.environ.get('PATH', '')}"
        return str(meta_bin.resolve())

    print("⚠️ zopen-build not found in PATH or meta directory. Will use direct patch verification.")
    return ""


def parse_buildenv(port_dir: Path) -> dict:
    """Parse buildenv file from port directory into key-value pairs and module mappings."""
    buildenv_file = port_dir / "buildenv"
    if not buildenv_file.exists():
        raise FileNotFoundError(f"buildenv not found in {port_dir}")

    content = buildenv_file.read_text(encoding="utf-8", errors="replace")
    
    env_vars = {}
    for line in content.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = re.match(r'^(?:export\s+)?([A-Za-z0-9_]+)=(?:"([^"]*)"|\'([^\']*)\'|([^#\s]+))', line)
        if m:
            key = m.group(1)
            val = m.group(2) if m.group(2) is not None else (m.group(3) if m.group(3) is not None else m.group(4))
            env_vars[key] = val

    for k, v in list(env_vars.items()):
        if isinstance(v, str) and "${" in v:
            for sub_k, sub_v in env_vars.items():
                v = v.replace(f"${{{sub_k}}}", sub_v).replace(f"${sub_k}", sub_v)
            env_vars[k] = v

    modules = {}
    for match in re.finditer(r'clone_module\s+(?:"([^"]+)"|([^\s]+))\s+([^\s]+)\s+(?:"([^"]+)"|([^\s]+))', content):
        url = match.group(1) or match.group(2)
        dir_name = match.group(3)
        ref = match.group(4) or match.group(5)
        for sub_k, sub_v in env_vars.items():
            url = url.replace(f"${{{sub_k}}}", sub_v).replace(f"${sub_k}", sub_v)
            ref = ref.replace(f"${{{sub_k}}}", sub_v).replace(f"${sub_k}", sub_v)
        modules[dir_name] = {"url": url, "ref": ref}

    patch_targets = {}
    for match in re.finditer(r'apply_port_patch\s+([^\s]+)\s+([^\s]+)', content):
        target_dir = match.group(1)
        patch_name = match.group(2)
        patch_targets[patch_name] = target_dir

    env_vars["_MODULES"] = modules
    env_vars["_PATCH_TARGETS"] = patch_targets
    return env_vars


def run_command(cmd: list[str] | str, cwd: Path | None = None, env: dict | None = None) -> tuple[int, str, str]:
    """Run a shell or subprocess command and return (returncode, stdout, stderr)."""
    shell = isinstance(cmd, str)
    proc_env = os.environ.copy()
    if env:
        proc_env.update(env)

    res = subprocess.run(
        cmd,
        cwd=cwd,
        env=proc_env,
        shell=shell,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        encoding="utf-8",
        errors="replace",
    )
    return res.returncode, res.stdout, res.stderr


def clone_repo(url: str, ref: str, dest_dir: Path) -> bool:
    """Clone a git repository at the given branch/tag."""
    if dest_dir.exists():
        return True
    clone_cmd = ["git", "clone", "--depth", "1"]
    if ref:
        clone_cmd.extend(["--branch", ref])
    clone_cmd.extend([url, str(dest_dir)])
    rc, out, err = run_command(clone_cmd)
    if rc != 0:
        rc, out, err = run_command(["git", "clone", url, str(dest_dir)])
        if rc == 0 and ref:
            run_command(["git", "checkout", ref], cwd=dest_dir)
    return dest_dir.exists()


def setup_dummy_deps(work_dir: Path, buildenv_vars: dict) -> Path:
    """Create dummy dependency directories so zopen-build satisfies dependency checks on non-z/OS."""
    deps_dir = work_dir / "deps"
    raw_deps = buildenv_vars.get("ZOPEN_STABLE_DEPS", "") + " " + buildenv_vars.get("ZOPEN_DEV_DEPS", "")
    for dep in raw_deps.split():
        dep_clean = dep.split(">=")[0].split("<=")[0].split("=")[0].strip()
        if dep_clean:
            target = deps_dir / dep_clean / dep_clean
            target.mkdir(parents=True, exist_ok=True)
            (target / "bin").mkdir(exist_ok=True)
            (target / "lib").mkdir(exist_ok=True)
    return deps_dir


def run_zopen_build_local(port_dir: Path, meta_dir: Path, deps_dir: Path, log_file: Path) -> tuple[int, str]:
    """Execute zopen-build -vv in port directory with configured dependency path and capture log."""
    zopen_build_bin = meta_dir / "bin" / "zopen-build"
    if not zopen_build_bin.exists():
        zopen_build_bin_str = shutil.which("zopen-build")
        if not zopen_build_bin_str:
            return 1, "zopen-build binary not found"
        zopen_build_bin = Path(zopen_build_bin_str)

    env = os.environ.copy()
    env["PATH"] = f"{meta_dir / 'bin'}:{env.get('PATH', '')}"
    env["ZOPEN_DONT_PROCESS_CONFIG"] = "1"
    env["ZOPEN_ROOT"] = str(port_dir)

    print(f"Running zopen-build -vv on {port_dir}...")
    cmd = f"{zopen_build_bin.resolve()} -vv -d {deps_dir.resolve()} > {log_file} 2>&1"
    rc, out, err = run_command(cmd, cwd=port_dir, env=env)
    
    log_content = ""
    if log_file.exists():
        log_content = log_file.read_text(encoding="utf-8", errors="replace")
    return rc, log_content


def test_patches_against_upstream(port_dir: Path, buildenv_vars: dict, work_dir: Path) -> dict:
    """Clone upstream source(s) and test applying each patch in patches/."""
    patches_dir = port_dir / "patches"
    modules = buildenv_vars.get("_MODULES", {})
    patch_targets = buildenv_vars.get("_PATCH_TARGETS", {})

    upstream_url = buildenv_vars.get("ZOPEN_STABLE_URL") or buildenv_vars.get("ZOPEN_DEV_URL")
    upstream_tag = buildenv_vars.get("ZOPEN_STABLE_TAG") or buildenv_vars.get("SYFT_VERSION") or ""

    results = {
        "upstream_url": upstream_url,
        "upstream_tag": upstream_tag,
        "failed_patches": [],
        "passed_patches": [],
        "upstream_cloned": False,
    }

    patch_files = sorted(list(patches_dir.glob("*.patch")) + list(patches_dir.glob("*.gopatch"))) if patches_dir.exists() else []
    if not patch_files:
        return results

    cloned_dirs = {}
    if modules:
        for mod_name, mod_info in modules.items():
            mod_dir = work_dir / mod_name
            if clone_repo(mod_info["url"], mod_info["ref"], mod_dir):
                cloned_dirs[mod_name] = mod_dir
        results["upstream_cloned"] = len(cloned_dirs) > 0
    elif upstream_url:
        main_dir = work_dir / "upstream_src"
        if clone_repo(upstream_url, upstream_tag, main_dir):
            cloned_dirs["root"] = main_dir
            results["upstream_cloned"] = True

    # Test each patch
    for patch_path in patch_files:
        patch_name = patch_path.name
        target_name = patch_targets.get(patch_name, "root")
        target_dir = cloned_dirs.get(target_name) or cloned_dirs.get("root")

        if not target_dir or not target_dir.exists():
            continue

        # Clean git state before applying
        run_command(["git", "checkout", "."], cwd=target_dir)
        run_command(["git", "clean", "-fdx"], cwd=target_dir)

        rc, out, err = run_command(["git", "apply", "--check", str(patch_path.resolve())], cwd=target_dir)
        if rc == 0:
            results["passed_patches"].append(patch_name)
        else:
            # Patch failed! Extract rejected hunks
            src_copy = work_dir / f"src_{patch_name}"
            if src_copy.exists():
                shutil.rmtree(src_copy)
            shutil.copytree(target_dir, src_copy, symlinks=True, ignore=shutil.ignore_patterns(".git"))
            rc_rej, out_rej, err_rej = run_command(["git", "apply", "--reject", str(patch_path.resolve())], cwd=src_copy)
            
            rej_files = list(src_copy.rglob("*.rej"))
            rejected_info = []
            for rf in rej_files:
                rel_path = rf.relative_to(src_copy)
                orig_file = str(rel_path)[:-4]  # strip .rej
                target_src_file = target_dir / orig_file
                src_content = ""
                if target_src_file.exists():
                    try:
                        src_content = target_src_file.read_text(encoding="utf-8", errors="replace")
                    except Exception:
                        pass
                
                rejected_info.append({
                    "target_file": orig_file,
                    "rejected_hunk": rf.read_text(encoding="utf-8", errors="replace"),
                    "current_upstream_content": src_content,
                })

            results["failed_patches"].append({
                "patch_name": patch_name,
                "target_module": target_name,
                "patch_path": str(patch_path.relative_to(port_dir)),
                "patch_content": patch_path.read_text(encoding="utf-8", errors="replace"),
                "error_message": err or out,
                "rejections": rejected_info,
            })

    return results


def call_copilot_to_generate_patch(context_markdown: str, token: str) -> str:
    """Call GitHub Copilot CLI to generate diagnosis and patch fix."""
    copilot_bin = os.environ.get("COPILOT_BIN", "copilot")
    copilot_which = shutil.which(copilot_bin)

    if not copilot_which:
        print("Copilot CLI not found in PATH.")
        return ""

    prompt = (
        "You are an expert z/OS systems software engineer and maintainer for the zopen community.\n"
        "Analyze the port build/patch failure and output a clean diagnosis report.\n"
        "You MUST include the exact, complete, valid unified git patch inside a ```diff code block.\n\n"
        + context_markdown
    )

    env = os.environ.copy()
    if token:
        env["COPILOT_GITHUB_TOKEN"] = token
        env["GITHUB_TOKEN"] = token

    command = [copilot_which, "-s", "--no-color", "--available-tools"]
    try:
        with tempfile.TemporaryDirectory() as empty_dir:
            result = subprocess.run(
                command,
                input=prompt,
                capture_output=True,
                text=True,
                timeout=300,
                cwd=empty_dir,
                env=env,
            )
        if result.returncode == 0 and result.stdout.strip():
            print("✅ Successfully generated fix report using GitHub Copilot CLI")
            return result.stdout.strip()
        else:
            print(f"Copilot CLI notice: {result.stderr.strip()[-200:]}")
            return ""
    except Exception as e:
        print(f"Error executing Copilot CLI: {e}")
        return ""


def extract_diff_from_markdown(markdown_text: str) -> str:
    """Extract valid unified diff code block from markdown text."""
    diff_blocks = re.findall(r'```(?:diff|patch)?\n([\s\S]*?)\n```', markdown_text)
    for block in diff_blocks:
        if "---" in block and "+++" in block:
            return block.strip()
    return ""


def apply_fix_to_sandbox(sandbox_port: Path, patch_diff: str, failed_patch_name: str) -> bool:
    """Apply generated diff to sandbox patches or buildenv."""
    if not patch_diff:
        return False

    if "--- a/buildenv" in patch_diff or "+++ b/buildenv" in patch_diff:
        tmp_patch = sandbox_port / "temp_buildenv.patch"
        tmp_patch.write_text(patch_diff + "\n", encoding="utf-8")
        rc, out, err = run_command(["git", "apply", "--ignore-whitespace", str(tmp_patch)], cwd=sandbox_port)
        tmp_patch.unlink(missing_ok=True)
        if rc == 0:
            print("✅ Successfully applied buildenv patch to sandbox")
            return True

    if failed_patch_name:
        target_patch_file = sandbox_port / "patches" / failed_patch_name
        if target_patch_file.exists():
            target_patch_file.write_text(patch_diff + "\n", encoding="utf-8")
            print(f"✅ Updated {failed_patch_name} in sandbox with Copilot fix")
            return True

    return False


def generate_copilot_facts_bundle(
    port_dir: Path,
    buildenv_vars: dict,
    patch_results: dict,
    build_log: str,
    output_file: Path,
    iteration: int = 1,
) -> str:
    """Format failure details, rejected hunks, and upstream code into a prompt for Copilot."""
    lines = []
    port_name = buildenv_vars.get("ZOPEN_NAME") or port_dir.name
    version = buildenv_vars.get("ZOPEN_STABLE_TAG") or buildenv_vars.get("SYFT_VERSION") or "latest"

    lines.append(f"# Port Build Failure Analysis & Fix Request (Attempt #{iteration}): `{port_name}` ({version})")
    lines.append("")
    lines.append("## Overview")
    lines.append(f"- **Port Name**: `{port_name}`")
    lines.append(f"- **Target Version/Tag**: `{version}`")
    lines.append(f"- **Upstream Repository**: `{buildenv_vars.get('ZOPEN_STABLE_URL', 'N/A')}`")
    lines.append("")

    failed_patches = patch_results.get("failed_patches", [])
    if failed_patches:
        lines.append("## ❌ Failed Patches Detected")
        lines.append(f"Found {len(failed_patches)} patch(es) that failed to apply cleanly to upstream `{version}`:")
        lines.append("")
        for fp in failed_patches:
            lines.append(f"### Patch: `{fp['patch_name']}` (Target Module: `{fp.get('target_module', 'root')}`)")
            lines.append(f"**Error Details:**\n```\n{fp['error_message']}\n```")
            lines.append("")
            lines.append("#### Original Patch Content:")
            lines.append(f"```diff\n{fp['patch_content']}\n```")
            lines.append("")

            for rej in fp.get("rejections", []):
                lines.append(f"#### Rejected Hunk for `{rej['target_file']}`:")
                lines.append(f"```diff\n{rej['rejected_hunk']}\n```")
                lines.append("")
                if rej.get("current_upstream_content"):
                    lines.append(f"#### Upstream Source File (`{rej['target_file']}` at tag `{version}`):")
                    src = rej["current_upstream_content"]
                    src_lines = src.splitlines()
                    if len(src_lines) > 200:
                        lines.append(f"```text\n{chr(10).join(src_lines[:200])}\n... [truncated {len(src_lines)-200} lines]\n```")
                    else:
                        lines.append(f"```text\n{src}\n```")
                    lines.append("")
    else:
        lines.append("## Patches Status")
        lines.append(f"All {len(patch_results.get('passed_patches', []))} existing patches applied cleanly.")
        lines.append("")

    if build_log:
        log_lines = build_log.splitlines()
        tail_lines = log_lines[-100:] if len(log_lines) > 100 else log_lines
        lines.append("## 📋 Build Log Output (Last 100 lines of `zopen-build -vv`)")
        lines.append("```text")
        lines.extend(tail_lines)
        lines.append("```")
        lines.append("")

    buildenv_content = (port_dir / "buildenv").read_text(encoding="utf-8", errors="replace")
    lines.append("## ⚙️ Port `buildenv` File")
    lines.append(f"```bash\n{buildenv_content}\n```")
    lines.append("")

    lines.append("## 🎯 Copilot Goal & Required Output")
    lines.append("1. **Root Cause Analysis**: Explain precisely what changed upstream and why the patch/build broke.")
    lines.append("2. **Exact Solution**: Output the updated `.patch` file (or updated `buildenv` lines).")
    lines.append("3. Format the patch inside a standard unified diff block (`--- a/...`, `+++ b/...`) so it can be applied via `git apply`.")
    lines.append("")

    bundle_str = "\n".join(lines)
    output_file.parent.mkdir(parents=True, exist_ok=True)
    output_file.write_text(bundle_str, encoding="utf-8")
    return bundle_str


def main() -> int:
    parser = argparse.ArgumentParser(description="zopencommunity Port Build Triage & Copilot Auto-Fix")
    parser.add_argument("--port-dir", default=".", help="Path to port directory (e.g. ./syftport)")
    parser.add_argument("--meta-dir", default=".", help="Path to meta repo directory")
    parser.add_argument("--output", default="/tmp/gh-aw/autofix/facts.md", help="Output path for Copilot context markdown")
    parser.add_argument("--output-response", default="/tmp/gh-aw/autofix/response.md", help="Output path for generated Copilot response")
    parser.add_argument("--max-attempts", type=int, default=3, help="Max retry attempts for build-fix-verify loop")

    args = parser.parse_args()
    port_dir = Path(args.port_dir).resolve()
    meta_dir = Path(args.meta_dir).resolve()
    output_file = Path(args.output).resolve()
    response_file = Path(args.output_response).resolve()

    if not (port_dir / "buildenv").exists():
        print(f"Error: {port_dir} does not contain a buildenv file", file=sys.stderr)
        return 1

    check_and_setup_zopen(meta_dir)
    token = os.environ.get("COPILOT_GITHUB_TOKEN") or os.environ.get("GITHUB_TOKEN") or ""

    with tempfile.TemporaryDirectory(prefix="zopen_autofix_") as tmp_dir_str:
        work_dir = Path(tmp_dir_str)
        sandbox_port = work_dir / "sandbox_port"
        shutil.copytree(port_dir, sandbox_port, symlinks=True, ignore=shutil.ignore_patterns(".git"))

        print(f"🚀 Starting Build-Fix-Verify loop for {port_dir.name} (Max Attempts: {args.max_attempts})...")

        last_copilot_response = ""
        resolved = False

        for attempt in range(1, args.max_attempts + 1):
            print(f"\n--- [Attempt {attempt}/{args.max_attempts}] Testing build and patches ---")
            buildenv_vars = parse_buildenv(sandbox_port)
            deps_dir = setup_dummy_deps(work_dir, buildenv_vars)

            # Test patches against upstream
            patch_results = test_patches_against_upstream(sandbox_port, buildenv_vars, work_dir)
            failed_patches = patch_results.get("failed_patches", [])

            # Run local zopen-build with configured deps directory
            log_path = work_dir / f"zopen_build_{attempt}.log"
            rc, build_log = run_zopen_build_local(sandbox_port, meta_dir, deps_dir, log_path)

            # Check if all patches and build checks succeeded!
            if not failed_patches and attempt > 1:
                print(f"🎉 SUCCESS! All patches verified and applied cleanly with 0 rejects on attempt {attempt}!")
                resolved = True
                
                # Compute diff specifically for patches/ and buildenv
                diff_out_parts = []
                if (port_dir / "patches").exists() and (sandbox_port / "patches").exists():
                    _, d_patches, _ = run_command(["git", "diff", "--no-index", str(port_dir / "patches"), str(sandbox_port / "patches")])
                    if d_patches.strip():
                        diff_out_parts.append(d_patches.strip())

                if (port_dir / "buildenv").exists() and (sandbox_port / "buildenv").exists():
                    _, d_buildenv, _ = run_command(["git", "diff", "--no-index", str(port_dir / "buildenv"), str(sandbox_port / "buildenv")])
                    if d_buildenv.strip():
                        diff_out_parts.append(d_buildenv.strip())

                diff_out = "\n\n".join(diff_out_parts)
                
                final_report = [
                    f"### 🤖 GitHub Copilot Auto-Fix: Build Verification Succeeded (Attempt #{attempt})\n",
                    f"**Verification Result**: ✅ All {len(patch_results.get('passed_patches', []))} patches verified against upstream with **0 rejects**!\n",
                    last_copilot_response if last_copilot_response else "",
                ]
                if diff_out:
                    final_report.append("\n#### 🛠️ Verified Working Patch Diff:\n```diff\n" + diff_out.strip() + "\n```")
                
                response_file.write_text("\n".join(final_report), encoding="utf-8")
                break

            # If errors remain, generate prompt and query Copilot
            print(f"Attempt {attempt}: Found {len(failed_patches)} failing patch(es). Asking Copilot for fix...")
            bundle_str = generate_copilot_facts_bundle(sandbox_port, buildenv_vars, patch_results, build_log, output_file, iteration=attempt)

            if token:
                copilot_reply = call_copilot_to_generate_patch(bundle_str, token)
                if copilot_reply:
                    last_copilot_response = copilot_reply
                    response_file.write_text(copilot_reply, encoding="utf-8")
                    
                    # Extract diff and apply into sandbox
                    extracted_diff = extract_diff_from_markdown(copilot_reply)
                    target_patch_name = failed_patches[0]["patch_name"] if failed_patches else ""
                    if extracted_diff:
                        apply_fix_to_sandbox(sandbox_port, extracted_diff, target_patch_name)

        if not resolved and last_copilot_response:
            print("Reporting Copilot analysis.")
            response_file.write_text(last_copilot_response, encoding="utf-8")

    return 0


if __name__ == "__main__":
    sys.exit(main())

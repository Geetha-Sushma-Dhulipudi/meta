---
description: |
  Analyzes zopen port build and patch failures upon new version bumps or PR runs,
  identifies why patches rejected or builds broke, and automatically generates
  corrected unified git patch files and buildenv updates. Triggered automatically
  on build failures, workflow_dispatch, or via slash command (/fix or /autofix).

on:
  workflow_dispatch:
    inputs:
      repo:
        description: 'Port repository to analyze (e.g. syftport or zopencommunity/syftport)'
        required: true
        type: string
      pr_number:
        description: 'PR number to analyze and post fix to'
        required: false
        type: string
        default: ''
  slash_command:
    name: fix
    events: [issue_comment, pull_request_review_comment]
    strategy: centralized
  roles: all
  reaction: eyes

concurrency:
  job-discriminator: ${{ github.event.issue.number || github.event.pull_request.number || github.event.inputs.repo || github.run_id }}

user-rate-limit:
  max-runs-per-window: 5
  window: 60

permissions:
  contents: read
  issues: write
  pull-requests: write

network: defaults

engine:
  id: copilot
  model: gpt-4.1

tools:
  github: false
  bash: ["cat"]

safe-outputs:
  add-comment:
    max: 1
  report-failure-as-issue: false
  threat-detection:
    model: gpt-4.1
    report-as-issue: false

steps:
  - name: Gather port build and patch failure facts
    env:
      REPO: ${{ github.event.inputs.repo }}
      PR_NUMBER: ${{ github.event.inputs.pr_number || github.event.issue.number || github.event.pull_request.number }}
      AW_CONTEXT: ${{ github.event.inputs.aw_context }}
      GITHUB_TOKEN: ${{ github.token }}
    run: |
      mkdir -p /tmp/gh-aw/autofix
      TARGET_REPO="${REPO}"
      if [ -z "$TARGET_REPO" ] && [ -n "$AW_CONTEXT" ]; then
        TARGET_REPO=$(python3 -c "import json, sys; ctx=json.loads(sys.argv[1]); import re; m=re.search(r'/(?:autofix|fix)\s+([a-zA-Z0-9_\-\.\/]+)', ctx.get('comment_body','')); print(m.group(1) if m else '')" "$AW_CONTEXT" 2>/dev/null || true)
      fi
      if [ -z "$TARGET_REPO" ]; then
        TARGET_REPO="${{ github.repository }}"
      fi

      echo "Analyzing target repository: ${TARGET_REPO}"
      # Clone target port repo if not local
      PORT_DIR="/tmp/gh-aw/port_checkout"
      rm -rf "${PORT_DIR}"
      git clone "https://github.com/${TARGET_REPO}.git" "${PORT_DIR}"
      
      python3 tools/port_build_autofix.py --port-dir "${PORT_DIR}" --meta-dir . --output /tmp/gh-aw/autofix/facts.md --skip-build

timeout-minutes: 10
---

# zopen port build autofix assistant

You are an expert z/OS systems software engineer and maintainer for the **zopen community** (porting open source software to z/OS UNIX).

Your task is to analyze why a port build or patch failed after a version bump, and produce the exact corrected patch file and `buildenv` fix.

1. Run `cat /tmp/gh-aw/autofix/facts.md` once. Do not run any other command.
2. Post **one** comment using `add_comment` containing the root-cause diagnosis and the complete, valid unified git patch.

### Output Comment Format

Post your comment exactly in this format:

```markdown
### 🤖 Copilot Auto-Fix & Triage Report

#### 🔍 Root Cause Analysis
- **Problem**: [Explain which patch rejected or what broke in the new version]
- **Upstream Change**: [Explain what function, file, or line changed in upstream source]

#### 🛠️ Proposed Solution & Patch Fix
Apply the following patch to the repository (in `patches/<patch_name>` or `buildenv`):

\`\`\`diff
[Complete, valid unified git diff that applies cleanly with git apply]
\`\`\`

#### 📋 Next Steps
1. Apply the patch to the branch: `git apply <patch_file>`
2. Re-trigger the build: `/run tests`
```

### Guidelines for z/OS Patches:
- Ensure diff line numbers and context lines match the upstream source shown in the facts file.
- Keep z/OS-specific definitions (`#ifdef __MVS__`, `#cgo zos LDFLAGS`, etc.) intact while adapting them to the new upstream signature.

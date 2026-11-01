# Release v0.2.0 — publish commands (run in Git Bash / PowerShell with gh installed)
#
# 1. Install the GitHub CLI once:  winget install GitHub.cli   then   gh auth login
# 2. From the repo root (release/ already contains the three artifacts):
gh release create v0.2.0 \
  "release/Jevora-IDE-Setup-0.2.0.exe" \
  "release/Jevora-IDE-Portable-0.2.0.exe" \
  "release/latest.yml" \
  --title "Jevora IDE 0.2.0 — first packaged release" \
  --notes-file "docs/RELEASE-NOTES-v0.2.0.md"
# The installer is ~248 MB; upload may take several minutes.
# After publishing, installed builds auto-detect v0.2.0; future releases
# (v0.2.1+) trigger the in-app "Restart to update" flow.

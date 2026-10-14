"""Command risk classification for the approval policy (auto | ask_on_risk | always_ask)."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal

RiskLevel = Literal["safe", "risky", "blocked"]

# Never allowed, whatever the policy.
_BLOCKED: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r":\(\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:"), "fork bomb"),
    (re.compile(r"\brm\s+(-[a-z]*\s+)*-[a-z]*[rf][a-z]*\s+(-[a-z]+\s+)*(/|/\*|~|~/|\$HOME|/\w+)\s*($|[;&|])", re.I),
     "recursive delete of a root/home directory"),
    (re.compile(r"\bmkfs(\.\w+)?\b"), "filesystem format"),
    (re.compile(r"\bdd\s+.*\bof=/dev/"), "raw device write"),
    (re.compile(r">\s*/dev/(sd|nvme|hd|disk)"), "raw device write"),
    (re.compile(r"\b(shutdown|reboot|halt|poweroff)\b"), "host power control"),
    (re.compile(r"\bchmod\s+-R\s+0?777\s+/(\s|$)"), "recursive chmod of /"),
    (re.compile(r"\bformat\s+[a-z]:", re.I), "drive format"),
    (re.compile(r"\b(rd|rmdir|del)\s+/s\s+(/q\s+)?[a-z]:\\?\s*($|&)", re.I), "recursive delete of a drive"),
    (re.compile(r"\b(diskpart|bcdedit|vssadmin\s+delete|cipher\s+/w)\b", re.I), "destructive system tool"),
    (re.compile(r"\breg\s+delete\b", re.I), "registry deletion"),
    (re.compile(r"/dev/tcp/|\bnc(at)?\s+.*-e\s|bash\s+-i\s+>&", re.I), "reverse shell"),
]

# Allowed only with approval under ask_on_risk.
_RISKY: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\brm\s+(-[a-z]*\s+)*-[a-z]*r", re.I), "recursive delete"),
    (re.compile(r"\b(rmdir|rd)\s+/s\b|\bremove-item\b.*-recurse", re.I), "recursive delete"),
    (re.compile(r"\bgit\s+(push|reset\s+--hard|clean\s+-[a-z]*f|checkout\s+--\s|rebase|filter-branch)\b"),
     "destructive or remote git operation"),
    (re.compile(r"\b(curl|wget|invoke-webrequest|iwr|ssh|scp|rsync|ftp|telnet)\b", re.I), "network egress"),
    (re.compile(r"\b(pip3?|python\s+-m\s+pip)\s+install\b|\b(npm|pnpm|yarn)\s+(install|add|i)\b|\bapt(-get)?\s+install\b",
                re.I), "package installation (network egress)"),
    (re.compile(r"\bsudo\b|\bsu\s+-?\w*\b|\brunas\b", re.I), "privilege escalation"),
    (re.compile(r"\b(chmod|chown|icacls|takeown)\b", re.I), "permission change"),
    (re.compile(r"\b(kill|pkill|killall|taskkill)\b", re.I), "process termination"),
    (re.compile(r"\bdocker\b|\bkubectl\b", re.I), "container control"),
    (re.compile(r"(\.env\b|id_rsa|id_ed25519|\.aws/credentials|\.ssh/|secret|\.pem\b|\.key\b)", re.I),
     "touches secrets"),
    (re.compile(r"\b(printenv|env)\s*($|\|)|\bset\s*$|get-childitem\s+env:", re.I), "environment dump"),
    (re.compile(r">\s*/etc/|\btee\s+/etc/", re.I), "system configuration write"),
    (re.compile(r"\|\s*(sh|bash|zsh|python3?|powershell)\b", re.I), "piping into an interpreter"),
]


@dataclass
class RiskAssessment:
    level: RiskLevel
    reasons: list[str] = field(default_factory=list)

    @property
    def blocked(self) -> bool:
        return self.level == "blocked"


def classify_command(command: str) -> RiskAssessment:
    text = command.strip()
    blocked = [reason for pattern, reason in _BLOCKED if pattern.search(text)]
    if blocked:
        return RiskAssessment("blocked", sorted(set(blocked)))
    risky = [reason for pattern, reason in _RISKY if pattern.search(text)]
    if risky:
        return RiskAssessment("risky", sorted(set(risky)))
    return RiskAssessment("safe", [])


def needs_approval(policy: str, assessment: RiskAssessment, destructive: bool = False) -> bool:
    if assessment.blocked:
        return False  # blocked outright, never asked
    if policy == "always_ask":
        return True
    if policy == "ask_on_risk":
        return assessment.level == "risky" or destructive
    return False

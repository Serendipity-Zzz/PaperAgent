from __future__ import annotations

import re
import shutil
import subprocess
from enum import StrEnum
from pathlib import Path
from typing import ClassVar

from pydantic import BaseModel


class FindingSeverity(StrEnum):
    INFO = "info"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    BLOCKING = "blocking"


class SecurityFinding(BaseModel):
    tool: str
    rule_id: str
    severity: FindingSeverity
    path: str
    line: int | None = None
    message: str
    waived: bool = False


class ToolStatus(BaseModel):
    name: str
    available: bool
    executable: str | None = None
    degraded_reason: str | None = None


class SecurityReport(BaseModel):
    scanner_version: str = "1.0"
    findings: list[SecurityFinding]
    tools: list[ToolStatus]
    licenses: list[str]
    binary_files: list[str]
    requested_permissions: list[str]
    approved: bool = False

    @property
    def blocked(self) -> bool:
        return any(
            item.severity in {FindingSeverity.HIGH, FindingSeverity.BLOCKING} and not item.waived
            for item in self.findings
        )


RULES = {
    "recursive-delete": (
        r"(?:rm\s+-rf|Remove-Item[^\n]+-Recurse|shutil\.rmtree)",
        FindingSeverity.HIGH,
    ),
    "registry-write": (r"(?:reg\s+add|winreg\.(?:SetValue|CreateKey))", FindingSeverity.HIGH),
    "path-mutation": (r"setx\s+PATH|os\.environ\[['\"]PATH", FindingSeverity.HIGH),
    "download-execute": (
        r"(?:Invoke-WebRequest|curl|wget)[^\n|;&]*(?:\||;|&&)[^\n]*(?:powershell|python|bash|sh)",
        FindingSeverity.BLOCKING,
    ),
    "credential-access": (
        r"(?:\.ssh|Credentials|Login Data|credential manager|api[_-]?key)",
        FindingSeverity.HIGH,
    ),
    "dynamic-exec": (r"\b(?:eval|exec)\s*\(", FindingSeverity.MEDIUM),
    "disable-security": (r"Set-MpPreference|DisableRealtimeMonitoring", FindingSeverity.BLOCKING),
}


class SkillSecurityScanner:
    TEXT_SUFFIXES: ClassVar[set[str]] = {
        ".py",
        ".ps1",
        ".sh",
        ".js",
        ".ts",
        ".md",
        ".yaml",
        ".yml",
        ".toml",
        ".json",
    }

    def tool_status(self) -> list[ToolStatus]:
        names = {
            "defender": "MpCmdRun.exe",
            "semgrep": "semgrep",
            "bandit": "bandit",
            "osv-scanner": "osv-scanner",
            "pip-audit": "pip-audit",
        }
        return [
            ToolStatus(
                name=name,
                available=bool(path := shutil.which(executable)),
                executable=path,
                degraded_reason=None
                if path
                else "tool not installed; custom deterministic scan still ran",
            )
            for name, executable in names.items()
        ]

    def scan(self, root: Path, *, requested_permissions: list[str] | None = None) -> SecurityReport:
        findings: list[SecurityFinding] = []
        binaries: list[str] = []
        licenses: list[str] = []
        for file in root.rglob("*"):
            if not file.is_file() or ".git" in file.parts:
                continue
            relative = file.relative_to(root).as_posix()
            if file.name.casefold().startswith(("license", "copying", "notice")):
                licenses.append(relative)
            if file.suffix.lower() not in self.TEXT_SUFFIXES:
                binaries.append(relative)
                if file.suffix.lower() in {".exe", ".dll", ".msi", ".bat", ".cmd"}:
                    findings.append(
                        SecurityFinding(
                            tool="custom",
                            rule_id="binary",
                            severity=FindingSeverity.HIGH,
                            path=relative,
                            message="Executable or binary payload requires separate approval",
                        )
                    )
                continue
            text = file.read_text(encoding="utf-8", errors="replace")
            for rule_id, (pattern, severity) in RULES.items():
                for match in re.finditer(pattern, text, re.IGNORECASE):
                    findings.append(
                        SecurityFinding(
                            tool="custom",
                            rule_id=rule_id,
                            severity=severity,
                            path=relative,
                            line=text.count("\n", 0, match.start()) + 1,
                            message=f"Matched deterministic dangerous behavior rule: {rule_id}",
                        )
                    )
            for match in re.finditer(r"(?:sk-|ghp_|AKIA)[A-Za-z0-9_-]{12,}", text):
                findings.append(
                    SecurityFinding(
                        tool="custom",
                        rule_id="secret",
                        severity=FindingSeverity.BLOCKING,
                        path=relative,
                        line=text.count("\n", 0, match.start()) + 1,
                        message="Possible embedded credential",
                    )
                )
        return SecurityReport(
            findings=findings,
            tools=self.tool_status(),
            licenses=licenses,
            binary_files=binaries,
            requested_permissions=requested_permissions or [],
        )

    def scan_external(self, root: Path, *, timeout: int = 180) -> list[SecurityFinding]:
        findings: list[SecurityFinding] = []
        commands = {
            "defender": ["MpCmdRun.exe", "-Scan", "-ScanType", "3", "-File", str(root)],
            "semgrep": ["semgrep", "scan", "--json", str(root)],
            "bandit": ["bandit", "-r", str(root), "-f", "json"],
            "osv-scanner": ["osv-scanner", "scan", "--format", "json", str(root)],
            "pip-audit": ["pip-audit", "--format", "json"],
        }
        for status in self.tool_status():
            if not status.available or not status.executable:
                continue
            command = commands[status.name]
            command[0] = status.executable
            try:
                result = subprocess.run(
                    command,
                    cwd=root,
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                    check=False,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            except subprocess.TimeoutExpired:
                findings.append(
                    SecurityFinding(
                        tool=status.name,
                        rule_id="scanner-timeout",
                        severity=FindingSeverity.MEDIUM,
                        path=".",
                        message=f"{status.name} exceeded {timeout} seconds",
                    )
                )
                continue
            if result.returncode not in {0, 1}:
                findings.append(
                    SecurityFinding(
                        tool=status.name,
                        rule_id="scanner-error",
                        severity=FindingSeverity.MEDIUM,
                        path=".",
                        message=(result.stderr or result.stdout)[-500:],
                    )
                )
            elif result.returncode == 1:
                findings.append(
                    SecurityFinding(
                        tool=status.name,
                        rule_id="external-finding",
                        severity=FindingSeverity.HIGH,
                        path=".",
                        message=f"{status.name} reported findings; inspect its JSON log",
                    )
                )
        return findings

    @staticmethod
    def waive(report: SecurityReport, rule_id: str) -> SecurityReport:
        updated = report.model_copy(deep=True)
        for finding in updated.findings:
            if finding.rule_id == rule_id and finding.severity is not FindingSeverity.BLOCKING:
                finding.waived = True
        return updated

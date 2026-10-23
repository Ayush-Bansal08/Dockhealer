"""Vulnerability scan of a built image with Trivy (optional: skipped cleanly when Trivy is not installed).

WHAT A SCAN PROVES: the packages found in the image (OS packages and language dependencies) match known CVE
records in the scanner's database AS OF THE SCAN TIME.
WHAT IT DOES NOT PROVE: that the image is secure. It cannot see: unknown (zero-day) flaws, bugs in your own
code, weak configuration, secrets baked into files (we scan vulnerabilities only), or anything the database
does not know yet. "0 findings" means "nothing known was matched", never "safe". Findings with no fixed
version are often not actionable; many findings are unreachable in practice. Treat the numbers as a signal
to triage, not a verdict.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import asdict, dataclass, field
from typing import Callable

SEVERITY_ORDER = ["CRITICAL", "HIGH", "MEDIUM", "LOW", "UNKNOWN"]


@dataclass
class ScanResult:
    available: bool                      # False when Trivy is not installed
    ok: bool                             # scan completed and output was parsed
    counts: dict = field(default_factory=dict)      # severity -> number of findings (only the severities requested)
    total: int = 0
    fixable: int = 0                     # findings that have a fixed version available
    top: list = field(default_factory=list)         # most severe findings, capped
    severities: list = field(default_factory=list)  # which severities were requested
    scanner: str = "trivy"
    detail: str = ""


def parse_trivy_json(text: str, severities: list[str] | None = None, top_n: int = 10) -> ScanResult:
    data = json.loads(text)
    vulns = []
    for res in (data.get("Results") or []):
        for v in (res.get("Vulnerabilities") or []):          # may be null when a target has none
            vulns.append({"id": v.get("VulnerabilityID", "?"), "package": v.get("PkgName", "?"),
                          "installed": v.get("InstalledVersion", ""), "fixed": v.get("FixedVersion") or "",
                          "severity": (v.get("Severity") or "UNKNOWN").upper(), "target": res.get("Target", "")})
    rank = {s: i for i, s in enumerate(SEVERITY_ORDER)}
    vulns.sort(key=lambda v: (rank.get(v["severity"], 99), v["package"], v["id"]))
    counts = {s: sum(1 for v in vulns if v["severity"] == s) for s in SEVERITY_ORDER if any(v["severity"] == s for v in vulns)}
    return ScanResult(available=True, ok=True, counts=counts, total=len(vulns),
                      fixable=sum(1 for v in vulns if v["fixed"]), top=vulns[:top_n], severities=list(severities or []))


def scan_image(tag: str, *, severities: tuple[str, ...] = ("HIGH", "CRITICAL"), timeout_s: float = 300,
               runner: Callable = subprocess.run, which: Callable = shutil.which) -> ScanResult:
    sev = list(severities)
    if not which("trivy"):
        return ScanResult(available=False, ok=False, severities=sev,
                          detail="trivy is not installed: scan skipped (install it from Trivy's official docs to enable)")
    cmd = ["trivy", "image", "--quiet", "--format", "json", "--scanners", "vuln",
           "--severity", ",".join(sev), "--timeout", f"{int(timeout_s)}s", tag]
    try:
        p = runner(cmd, capture_output=True, text=True, errors="replace", timeout=timeout_s + 60)
    except subprocess.TimeoutExpired:
        return ScanResult(available=True, ok=False, severities=sev, detail=f"trivy did not finish within {timeout_s:.0f}s")
    except OSError as e:
        return ScanResult(available=False, ok=False, severities=sev, detail=f"could not run trivy: {e}")
    if p.returncode != 0:
        tail = " ".join((p.stderr or "").strip().splitlines()[-3:])
        return ScanResult(available=True, ok=False, severities=sev,
                          detail=f"trivy failed (exit {p.returncode}); first run needs network to download its database. {tail}"[:400])
    try:
        return parse_trivy_json(p.stdout, sev)
    except (ValueError, AttributeError, TypeError) as e:
        return ScanResult(available=True, ok=False, severities=sev, detail=f"could not parse trivy output: {e}")


def summarize(r: ScanResult) -> str:
    if not r.available:
        return r.detail
    if not r.ok:
        return f"scan failed: {r.detail}"
    shown = ", ".join(f"{n} {s}" for s, n in r.counts.items()) or "none"
    return (f"{r.total} known vulnerabilities at severity {'/'.join(r.severities)} ({shown}); {r.fixable} have a fix available. "
            "This lists known CVEs only: it does not show the image is secure.")


def to_dict(r: ScanResult) -> dict:
    return asdict(r)


def to_dict_scan(tag: str) -> dict:
    """scan_fn for the healer: scan and return a JSON-friendly dict."""
    return to_dict(scan_image(tag))

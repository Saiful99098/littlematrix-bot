"""Personal FILTER system — SEPARATE from signal generation.

This is Saiful's Aston/Newexe filter, exactly as specified on
2026-11-22. It must NOT be added inside signal generation.
It lives here, behind its own FILTER button, and works on lists
the user pastes (Aston list + Newexe list).

Rules (UTC+6, M1):
1) Remove 14:00-19:00 signals from EACH list separately, without
   merging yet. 19:00 is treated as included (removed).
2) In EACH list separately: if two signals are 1-4 minutes apart,
   remove BOTH.
3) Merge the two filtered lists and sort by time ascending.
4) In the merged list: if two signals are 1-4 minutes apart,
   remove ONLY the Aston signal, keep the Newexe one.
   (Comparison is by time only, across assets.)
5) Final output: ONLY "ASSET-OTC,HH:MM M1 CALL/PUT" lines.
   No Aston/Newexe written after CALL/PUT.

Note on step 4: "1-4 minutes apart" means the absolute time
difference between neighbouring signals in the sorted merged list
is 1, 2, 3 or 4 minutes. 0 minutes (same minute, different asset)
is NOT removed by this rule — that case is handled by the
cross-asset filter in the generator, not here.
"""
from __future__ import annotations

import re

from .premium_emoji import pe

LINE_RE = re.compile(
    r"^\s*([A-Za-z0-9]+(?:[-_][A-Za-z]+)?)\s*,\s*(\d{1,2}:\d{2})\s+M1\s+(CALL|PUT)\s*$",
    re.IGNORECASE,
)


def parse_line(line):
    m = LINE_RE.match(line.strip())
    if not m:
        return None
    asset = m.group(1).upper()
    h, mi = m.group(2).split(":")
    hhmm = f"{int(h):02d}:{int(mi):02d}"
    return {"asset": asset, "hhmm": hhmm, "direction": m.group(3).upper()}


def _minutes(hhmm):
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def _in_blocked_window(hhmm):
    """14:00-19:00 inclusive."""
    mins = _minutes(hhmm)
    return 14 * 60 <= mins <= 19 * 60


def _remove_close_pairs(signals):
    """Step 2: in one list, remove BOTH signals of any 1-4 min pair.

    A signal is removed if it is 1-4 minutes from ANY neighbour.
    """
    ordered = sorted(signals, key=lambda s: _minutes(s["hhmm"]))
    drop = set()
    for i in range(len(ordered) - 1):
        diff = _minutes(ordered[i + 1]["hhmm"]) - _minutes(ordered[i]["hhmm"])
        if 1 <= diff <= 4:
            drop.add(i)
            drop.add(i + 1)
    return [s for i, s in enumerate(ordered) if i not in drop]


def filter_personal(aston_lines, newexe_lines):
    """Apply the full Aston/Newexe personal filter.

    Returns (final_lines, counts) where counts records how many were
    removed at each step for reporting.
    """
    aston = [p for p in (parse_line(x) for x in aston_lines) if p]
    newexe = [p for p in (parse_line(x) for x in newexe_lines) if p]
    counts = {"aston_in": len(aston), "newexe_in": len(newexe)}

    # Step 1: remove 14:00-19:00 from each list separately.
    aston = [s for s in aston if not _in_blocked_window(s["hhmm"])]
    newexe = [s for s in newexe if not _in_blocked_window(s["hhmm"])]
    counts["removed_14_19"] = (counts["aston_in"] + counts["newexe_in"]
                               - len(aston) - len(newexe))

    # Step 2: 1-4 min gap inside each list separately, remove both.
    before = len(aston) + len(newexe)
    aston = _remove_close_pairs(aston)
    newexe = _remove_close_pairs(newexe)
    counts["removed_gap_each"] = before - len(aston) - len(newexe)

    # Step 3: merge and sort by time.
    merged = []
    for s in aston:
        merged.append({**s, "source": "Aston"})
    for s in newexe:
        merged.append({**s, "source": "Newexe"})
    merged.sort(key=lambda s: (_minutes(s["hhmm"]), s["asset"]))

    # Step 4: in merged list, 1-4 min apart -> remove only Aston.
    drop_idx = set()
    for i in range(len(merged) - 1):
        diff = _minutes(merged[i + 1]["hhmm"]) - _minutes(merged[i]["hhmm"])
        if 1 <= diff <= 4:
            for j in (i, i + 1):
                if merged[j]["source"] == "Aston":
                    drop_idx.add(j)
    counts["removed_aston_merged"] = len(drop_idx)
    merged = [s for i, s in enumerate(merged) if i not in drop_idx]

    final = [f"{s['asset']},{s['hhmm']} M1 {s['direction']}" for s in merged]
    counts["final"] = len(final)
    return final, counts


def format_filter_result(final, counts):
    lines = list(final)
    lines.append("")
    lines.append(f"{pe('chart')} Aston in: {counts['aston_in']} | Newexe in: {counts['newexe_in']}")
    lines.append(f"Removed 14:00-19:00: {counts['removed_14_19']}")
    lines.append(f"Removed 1-4 min gap (each list): {counts['removed_gap_each']}")
    lines.append(f"Removed Aston after merge: {counts['removed_aston_merged']}")
    lines.append(f"{pe('check')} Final: {counts['final']}")
    return "\n".join(lines)

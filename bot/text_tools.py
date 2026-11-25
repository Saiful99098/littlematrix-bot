"""Text tools — no Quotex needed, pure string work."""
from __future__ import annotations

from datetime import datetime, timedelta

# ---- FORMATTER: a few common styles ----
_STYLES = {
    "plain": lambda s: s,
    "upper": lambda s: s.upper(),
    "lower": lambda s: s.lower(),
    "title": lambda s: s.title(),
}


def formatter(text, style="plain"):
    fn = _STYLES.get(style, _STYLES["plain"])
    return fn(text)


def format_signal_list(text):
    """Flip a pasted signal list to the OTHER asset format.

    A list in display format comes back in stored format, and
    vice versa (per line, so mixed lists work too):
      USDMXN-OTC,09:02 M1 CALL -> USDMXN_otc,09:02 M1 CALL
      USDMXN_otc,09:02 M1 CALL -> USDMXN-OTC,09:02 M1 CALL
    Assets without an OTC suffix (EURUSD) are left unchanged.
    Lines that cannot be parsed are returned upper-cased unchanged.
    """
    import re
    out = []
    for raw_line in (text or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        # Try to find HH:MM and CALL/PUT anywhere in the line.
        m_time = re.search(r"(\d{1,2}):(\d{1,2})", line)
        m_dir = re.search(r"\b(CALL|PUT)\b", line, re.IGNORECASE)
        if not m_time or not m_dir:
            out.append(line.upper())
            continue
        hhmm = f"{int(m_time.group(1)):02d}:{int(m_time.group(2)):02d}"
        direction = m_dir.group(1).upper()
        # Asset is the first token before the time, if any.
        before = line[:m_time.start()].strip(" ,|-_")
        asset = ""
        if before:
            # Take the last word before the time as the asset.
            parts = re.split(r"[\s,;]+", before)
            asset = parts[-1] if parts else ""
        if asset:
            asset_up = asset.upper().strip()
            # Flip the suffix: -OTC <-> _otc.
            if asset_up.endswith("-OTC"):
                asset_up = asset_up[:-4] + "_otc"
            elif asset_up.endswith("_OTC"):
                asset_up = asset_up[:-4] + "-OTC"
            elif asset_up.endswith("OTC"):
                asset_up = asset_up[:-3] + "-OTC"
            out.append(f"{asset_up},{hhmm} M1 {direction}")
        else:
            out.append(f"{hhmm} M1 {direction}")
    return "\n".join(out)


def swap_call_put(text):
    """Swap CALL <-> PUT in a signal list (placeholder-safe)."""
    return (text.replace("CALL", "\x00").replace("PUT", "CALL")
                .replace("\x00", "PUT"))


def convert_timezone(time_str, from_offset=6, to_offset=6):
    """Convert HH:MM from one UTC offset to another. Default UTC+6 -> UTC+6."""
    try:
        h, m = map(int, time_str.strip().split(":"))
    except ValueError:
        return time_str
    dt = datetime(2000, 1, 1, h, m) + timedelta(hours=to_offset - from_offset)
    return dt.strftime("%H:%M")


# ---- TEXT FONT: small unicode style maps ----
_FONT_MAPS = {
    "bold": str.maketrans(
        "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789",
        "𝐚𝐛𝐜𝐝𝐞𝐟𝐠𝐡𝐢𝐣𝐤𝐥𝐦𝐧𝐨𝐩𝐪𝐫𝐬𝐭𝐮𝐯𝐰𝐱𝐲𝐳𝐀𝐁𝐂𝐃𝐄𝐅𝐆𝐇𝐈𝐉𝐊𝐋𝐌𝐍𝐎𝐏𝐐𝐑𝐒𝐓𝐔𝐕𝐖𝐗𝐘𝐙𝟎𝟏𝟐𝟑𝟒𝟓𝟔𝟕𝟖𝟗"),
    "mono": str.maketrans(
        "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789",
        "𝚊𝚋𝚌𝚍𝚎𝚏𝚐𝚑𝚒𝚓𝚔𝚕𝚖𝚗𝚘𝚙𝚚𝚛𝚜𝚝𝚞𝚟𝚠𝚡𝚢𝚣𝙰𝙱𝙲𝙳𝙴𝙵𝙶𝙷𝙸𝙹𝙺𝙻𝙼𝙽𝙾𝙿𝚀𝚁𝚂𝚃𝚄𝚅𝚆𝚇𝚈𝚉𝟶𝟷𝟸𝟹𝟺𝟻𝟼𝟽𝟾𝟿"),
}


def text_font(text, style="bold"):
    table = _FONT_MAPS.get(style)
    if table is None:
        return text
    return text.translate(table)

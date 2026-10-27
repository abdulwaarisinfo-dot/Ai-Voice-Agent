"""Money is stored as integer minor units (e.g. paisa/cents) to avoid float errors."""


def format_money(minor: int, currency: str = "PKR") -> str:
    sign = "-" if minor < 0 else ""
    minor = abs(int(minor))
    return f"{sign}{currency} {minor // 100:,}.{minor % 100:02d}"

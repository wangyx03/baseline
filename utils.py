from zoneinfo import ZoneInfo

UTC_ZONE = ZoneInfo("UTC")
EASTERN_ZONE = ZoneInfo("America/New_York")


def format_et(dt):
    if dt is None:
        return ""

    utc_time = dt.replace(tzinfo=UTC_ZONE)
    eastern_time = utc_time.astimezone(EASTERN_ZONE)

    return eastern_time.strftime("%Y-%m-%d %H:%M:%S") + " ET"

from plugins.base_plugin.base_plugin import BasePlugin
from datetime import datetime, timedelta, date
from PIL import Image
import logging
import requests
import pytz
import icalendar
import recurring_ical_events

logger = logging.getLogger(__name__)

OPEN_METEO_FORECAST_URL = (
    "https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={long}"
    "&hourly=weather_code,temperature_2m"
    "&daily=weathercode,temperature_2m_max,temperature_2m_min,sunrise,sunset"
    "&current=temperature,windspeed,winddirection,is_day,precipitation,weather_code,apparent_temperature"
    "&timezone=auto&models=best_match&forecast_days={forecast_days}"
)
OPEN_METEO_UNIT_PARAMS = {
    "standard": "temperature_unit=celsius&wind_speed_unit=ms&precipitation_unit=mm",
    "metric": "temperature_unit=celsius&wind_speed_unit=ms&precipitation_unit=mm",
    "imperial": "temperature_unit=fahrenheit&wind_speed_unit=mph&precipitation_unit=inch",
}

UNITS = {
    "standard": {"temperature": "K", "speed": "m/s"},
    "metric": {"temperature": "\u00b0C", "speed": "m/s"},
    "imperial": {"temperature": "\u00b0F", "speed": "mph"},
}

# WMO weather-code -> (description, text symbol). Symbols chosen for e-ink legibility.
WMO_MAP = {
    0: ("Clear sky", "\u2600"),
    1: ("Mainly clear", "\U0001F324"),
    2: ("Partly cloudy", "\u26C5"),
    3: ("Overcast", "\u2601"),
    45: ("Fog", "\U0001F32B"),
    48: ("Icy fog", "\U0001F32B"),
    51: ("Light drizzle", "\U0001F326"),
    53: ("Drizzle", "\U0001F326"),
    55: ("Heavy drizzle", "\U0001F327"),
    56: ("Freezing drizzle", "\U0001F327"),
    57: ("Freezing drizzle", "\U0001F327"),
    61: ("Light rain", "\U0001F326"),
    63: ("Rain", "\U0001F327"),
    65: ("Heavy rain", "\U0001F327"),
    66: ("Freezing rain", "\U0001F327"),
    67: ("Freezing rain", "\U0001F327"),
    71: ("Light snow", "\U0001F328"),
    73: ("Snow", "\U0001F328"),
    75: ("Heavy snow", "\u2744"),
    77: ("Snow grains", "\U0001F328"),
    80: ("Light showers", "\U0001F326"),
    81: ("Showers", "\U0001F327"),
    82: ("Heavy showers", "\U0001F327"),
    85: ("Snow showers", "\U0001F328"),
    86: ("Snow showers", "\U0001F328"),
    95: ("Thunderstorm", "\u26C8"),
    96: ("Storm w/ hail", "\u26C8"),
    99: ("Storm w/ hail", "\u26C8"),
}


def describe_weather(code):
    try:
        code = int(code)
    except (TypeError, ValueError):
        return ("Unknown", "?")
    return WMO_MAP.get(code, ("Unknown", "?"))


class InkypiMultiview(BasePlugin):
    """Dashboard: digital clock (left), upcoming events (right), current weather (bottom)."""

    def generate_settings_template(self):
        template_params = super().generate_settings_template()
        template_params["style_settings"] = True
        return template_params

    def generate_image(self, settings, device_config):
        raw_lat = (settings.get("latitude") or "").strip()
        raw_long = (settings.get("longitude") or "").strip()
        if not raw_lat or not raw_long:
            raise RuntimeError("Latitude and Longitude are required.")
        try:
            lat = float(raw_lat)
            lon = float(raw_long)
        except (ValueError, TypeError):
            raise RuntimeError("Latitude and Longitude must be valid numbers.")

        units = settings.get("units") or "metric"
        if units not in ("metric", "imperial", "standard"):
            raise RuntimeError("Units must be metric, imperial or standard.")

        try:
            max_events = int(settings.get("maxEvents") or 5)
        except (ValueError, TypeError):
            max_events = 5
        max_events = max(1, min(10, max_events))

        try:
            lookahead_days = int(settings.get("lookaheadDays") or 14)
        except (ValueError, TypeError):
            lookahead_days = 14
        lookahead_days = max(1, min(30, lookahead_days))

        show_seconds = str(settings.get("showSeconds") or "false").lower() == "true"
        dashboard_title = (settings.get("dashboardTitle") or "").strip()

        timezone_name = device_config.get_config("timezone", default="America/New_York")
        time_format = device_config.get_config("time_format", default="12h")
        try:
            tz = pytz.timezone(timezone_name)
        except Exception:
            logger.warning("Invalid timezone %s, falling back to America/New_York", timezone_name)
            tz = pytz.timezone("America/New_York")

        now = datetime.now(tz)

        # --- Clock ---
        clock_time, clock_period = self.format_clock(now, time_format, show_seconds)
        clock_date = now.strftime("%A, %B %d")

        # --- Weather (Open-Meteo, no key) ---
        try:
            weather_raw = self.get_open_meteo_data(lat, lon, units, forecast_days=2)
            weather = self.parse_current_weather(weather_raw, units)
        except RuntimeError:
            raise
        except Exception as e:
            logger.error("Open-Meteo request failed: %s", str(e))
            raise RuntimeError("Weather request failed, please check logs.")

        # --- Events (iCal) ---
        calendar_urls = settings.get("calendarURLs[]") or settings.get("calendarURLs") or []
        if isinstance(calendar_urls, str):
            calendar_urls = [calendar_urls]
        # filter empties, also support comma-separated single inputs
        urls = []
        for u in calendar_urls:
            if not u:
                continue
            u = u.strip()
            if u:
                urls.append(u)

        events = []
        if urls:
            try:
                events = self.fetch_upcoming_events(urls, tz, now, lookahead_days, max_events, time_format)
            except RuntimeError:
                raise
            except Exception as e:
                logger.error("Calendar fetch failed: %s", str(e))
                raise RuntimeError("Failed to fetch calendar events, please check URLs/logs.")
        else:
            logger.info("No calendar URLs configured, showing empty events list.")

        dimensions = device_config.get_resolution()
        if device_config.get_config("orientation") == "vertical":
            dimensions = dimensions[::-1]

        template_params = {
            "dashboard_title": dashboard_title,
            "clock_time": clock_time,
            "clock_period": clock_period,
            "clock_date": clock_date,
            "events": events,
            "max_events": max_events,
            "weather": weather,
            "plugin_settings": settings,
        }

        image = self.render_image(dimensions, "dashboard.html", "dashboard.css", template_params)
        if not image:
            raise RuntimeError("Failed to take screenshot, please check logs.")
        if isinstance(image, Image.Image):
            return image
        return image

    # ---------- clock ----------
    def format_clock(self, now, time_format, show_seconds):
        if time_format == "24h":
            fmt = "%H:%M:%S" if show_seconds else "%H:%M"
            return (now.strftime(fmt), "")
        else:
            fmt = "%I:%M:%S" if show_seconds else "%I:%M"
            t = now.strftime(fmt).lstrip("0")
            if t.startswith(":"):
                t = "0" + t
            # edge: 12:xx lstrip would keep 12; midnight "00:xx" -> ":xx"? handle
            if not t or t[0] == ":":
                t = "12" + t
            return (t, now.strftime("%p"))

    def format_time(self, dt, time_format):
        if time_format == "24h":
            return dt.strftime("%H:%M")
        s = dt.strftime("%I:%M %p").lstrip("0")
        return s

    # ---------- weather ----------
    def get_open_meteo_data(self, lat, lon, units, forecast_days=2):
        unit_params = OPEN_METEO_UNIT_PARAMS[units]
        url = OPEN_METEO_FORECAST_URL.format(lat=lat, long=lon, forecast_days=forecast_days)
        url = url + "&" + unit_params
        resp = requests.get(url, timeout=30)
        if not 200 <= resp.status_code < 300:
            logger.error("Open-Meteo failed: %s", resp.content[:500])
            raise RuntimeError("Failed to retrieve weather data from Open-Meteo.")
        return resp.json()

    def parse_current_weather(self, data, units):
        current = data.get("current", {})
        daily = data.get("daily", {})
        temp = current.get("temperature")
        feels = current.get("apparent_temperature", temp)
        code = current.get("weather_code", 0)
        is_day = current.get("is_day", 1)
        wind = current.get("windspeed", 0)
        wind_dir = current.get("winddirection", 0)

        temp_conv = 273.15 if units == "standard" else 0.0
        if temp is not None:
            temp = round(temp + temp_conv)
        if feels is not None:
            feels = round(feels + temp_conv)

        tmax = daily.get("temperature_2m_max", [None])[0]
        tmin = daily.get("temperature_2m_min", [None])[0]
        if tmax is not None and units == "standard":
            tmax = round(tmax + 273.15)
        elif tmax is not None:
            tmax = round(tmax)
        if tmin is not None and units == "standard":
            tmin = round(tmin + 273.15)
        elif tmin is not None:
            tmin = round(tmin)

        desc, symbol = describe_weather(code)
        # night tweak for clear
        if is_day == 0 and code == 0:
            symbol = "\u263E"  # moon

        unit_label = UNITS[units]["temperature"]
        speed_label = UNITS[units]["speed"]
        arrow = self.get_wind_arrow(float(wind_dir or 0))

        return {
            "temp": temp if temp is not None else "--",
            "feels_like": feels if feels is not None else "--",
            "high": tmax if tmax is not None else "--",
            "low": tmin if tmin is not None else "--",
            "description": desc,
            "symbol": symbol,
            "unit": unit_label,
            "wind": wind,
            "wind_unit": speed_label,
            "wind_arrow": arrow,
            "degree_sign": "" if units == "standard" else "\u00b0",
        }

    def get_wind_arrow(self, wind_deg):
        dirs = [
            ("\u2193", 22.5), ("\u2199", 67.5), ("\u2190", 112.5),
            ("\u2196", 157.5), ("\u2191", 202.5), ("\u2197", 247.5),
            ("\u2192", 292.5), ("\u2198", 337.5), ("\u2193", 360.0),
        ]
        wind_deg = wind_deg % 360
        for arrow, bound in dirs:
            if wind_deg < bound:
                return arrow
        return "\u2191"

    # ---------- calendar ----------
    def fetch_upcoming_events(self, urls, tz, now, lookahead_days, max_events, time_format):
        start_range = now - timedelta(minutes=5)
        # strip tz for naive comparison? recurring_ical_events handles aware; use aware start/end
        end_range = now + timedelta(days=lookahead_days)
        parsed = []
        for url in urls:
            cal = self.fetch_calendar(url)
            try:
                events = recurring_ical_events.of(cal).between(start_range, end_range)
            except Exception as e:
                logger.error("Failed to expand events for %s: %s", url, str(e))
                raise RuntimeError(f"Failed to parse calendar events: {str(e)}")
            for ev in events:
                try:
                    title = str(ev.get("summary", "Untitled"))
                    dtstart = ev.decoded("dtstart")
                    dtend = ev.decoded("dtend") if "dtend" in ev else None
                except Exception:
                    continue
                all_day = False
                if isinstance(dtstart, datetime):
                    if dtstart.tzinfo is None:
                        dtstart = tz.localize(dtstart)
                    else:
                        dtstart = dtstart.astimezone(tz)
                    if dtend and isinstance(dtend, datetime):
                        if dtend.tzinfo is None:
                            dtend = tz.localize(dtend)
                        else:
                            dtend = dtend.astimezone(tz)
                    # skip events already ended
                    end_cmp = dtend if dtend else dtstart
                    if end_cmp < now - timedelta(minutes=5):
                        continue
                else:
                    # date = all-day
                    all_day = True
                    dtstart = datetime(dtstart.year, dtstart.month, dtstart.day, tzinfo=tz)
                    if dtend and not isinstance(dtend, datetime):
                        dtend = datetime(dtend.year, dtend.month, dtend.day, tzinfo=tz)
                    if dtstart.date() < now.date():
                        continue
                parsed.append({
                    "title": title,
                    "start": dtstart,
                    "end": dtend,
                    "all_day": all_day,
                })
        # sort + dedupe + limit
        parsed.sort(key=lambda e: e["start"])
        seen = set()
        unique = []
        for e in parsed:
            key = (e["title"], e["start"].isoformat())
            if key in seen:
                continue
            seen.add(key)
            unique.append(e)
            if len(unique) >= max_events:
                break

        # format for template
        today = now.date()
        tomorrow = today + timedelta(days=1)
        out = []
        for e in unique:
            s = e["start"]
            d = s.date()
            if d == today:
                day_label = "Today"
            elif d == tomorrow:
                day_label = "Tomorrow"
            else:
                day_label = s.strftime("%a %m/%d")
            if e["all_day"]:
                time_label = "All day"
            else:
                time_label = self.format_time(s, time_format)
                if e["end"] and isinstance(e["end"], datetime):
                    # only show end if same day
                    if e["end"].date() == d:
                        time_label += " - " + self.format_time(e["end"], time_format)
            out.append({
                "title": e["title"],
                "day_label": day_label,
                "time_label": time_label,
            })
        return out

    def fetch_calendar(self, calendar_url):
        if calendar_url.startswith("webcal://"):
            calendar_url = calendar_url.replace("webcal://", "https://")
        try:
            resp = requests.get(calendar_url, timeout=30)
            resp.raise_for_status()
            return icalendar.Calendar.from_ical(resp.text)
        except Exception as e:
            raise RuntimeError(f"Failed to fetch iCalendar url: {str(e)}")

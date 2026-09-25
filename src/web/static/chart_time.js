/* Time-axis labels for every chart on both pages.
 *
 * The library formats the time axis itself, and it is wrong twice over for this
 * application:
 *
 * 1. **It does not know the bar size.** With `timeVisible` left at its default the
 *    label is a DATE, so every candle in a session carries the same label and the
 *    axis says nothing about which minute, hour or day a candle actually is. This
 *    is the thing the axis exists to answer, and at maximum zoom it is the only
 *    thing that distinguishes one candle from its neighbour.
 * 2. **It formats in UTC.** The bars are stamped in the exchange's own local time
 *    (09:30 is half past nine in New York) and handed to the library as a UTC
 *    instant, so a UTC label renders a 09:30 New York candle as 13:30 — a time the
 *    market was not even open at that session. The table beside it says 09:30.
 *
 * So the labels are built here, from the bar's own stamp converted into
 * `MARKET_TIMEZONE`, and the caller passes that zone in. One module for the price
 * chart, every indicator pane and the report page, so no chart can disagree with
 * another about what time a candle is.
 *
 * The label is the bar's PERIOD — the stamp is the start of the interval the candle
 * covers, which is what "this candle is the 09:35 bar" means.
 */
(function (global) {
  "use strict";

  /* A calendar bar is keyed by its date alone, so its axis must not show a time of
   * day: a daily candle's stamp is midnight, and "00:00" beside a day's candle reads
   * like a real bar that traded at midnight.
   *
   * Mirrors `dataset._CALENDAR_BARS`, the server's single source. Pinned equal by
   * tests/test_web/test_chart_time.py, because this list deciding differently from
   * the server's would put a time on a daily axis (or hide it on an intraday one). */
  var CALENDAR_BARS = ["1d", "5d", "3d", "1W", "2W", "1M", "2M", "3M", "1Q"];

  function isIntraday(interval) {
    var text = interval == null ? "" : String(interval);
    return CALENDAR_BARS.indexOf(text) === -1;
  }

  /* `tickMarkType`, as the library numbers it. The library passes the value it has
   * chosen for this tick, so the label can get coarser as the chart is zoomed out
   * instead of every tick shouting a full timestamp. */
  var YEAR = 0, MONTH = 1, DAY_OF_MONTH = 2, TIME = 3, TIME_WITH_SECONDS = 4;

  var MONTH_NAMES = [
    "Jan", "Feb", "Mar", "Apr", "May", "Jun",
    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
  ];

  function pad(n) {
    return (n < 10 ? "0" : "") + n;
  }

  /* A calendar bar's time value, whichever form it arrives in: the 'YYYY-MM-DD'
   * string this app stores, or the {year, month, day} object the library hands a
   * formatter. Returns null for anything else (an intraday unix stamp). */
  function calendarParts(time) {
    if (time && typeof time === "object") {
      if (typeof time.year !== "number") return null;
      return { year: time.year, month: time.month, day: time.day };
    }
    var m = /^(\d{4})-(\d{2})-(\d{2})/.exec(String(time));
    if (!m) return null;
    return { year: +m[1], month: +m[2], day: +m[3] };
  }

  /* Formatters are cached: the axis asks for one label per tick, on every pan and
   * zoom, and building an Intl.DateTimeFormat is the expensive part. */
  var formats = {};

  function formatter(timeZone, options, key) {
    var id = key + "|" + timeZone;
    if (!formats[id]) {
      var opts = { hourCycle: "h23" };
      for (var k in options) if (Object.prototype.hasOwnProperty.call(options, k)) opts[k] = options[k];
      try {
        formats[id] = new Intl.DateTimeFormat("en-US", Object.assign({ timeZone: timeZone }, opts));
      } catch (err) {
        // An unrecognised zone must not blank the axis; fall back to the browser's.
        formats[id] = new Intl.DateTimeFormat("en-US", opts);
      }
    }
    return formats[id];
  }

  function zoneLabel(timeZone, options, key, date) {
    return formatter(timeZone, options, key).format(date);
  }

  /* "2026-09-18 09:30" — deliberately the same shape as the row labels the data
   * table and the Historical Delta panel show (`dataset.bar_label`), so the crosshair
   * and the tables name a bar identically. */
  function stampLabel(timeZone, date) {
    var bits = formatter(timeZone, {
      year: "numeric", month: "2-digit", day: "2-digit",
      hour: "2-digit", minute: "2-digit",
    }, "stamp").formatToParts(date);
    var found = {};
    for (var i = 0; i < bits.length; i++) found[bits[i].type] = bits[i].value;
    return found.year + "-" + found.month + "-" + found.day +
      " " + found.hour + ":" + found.minute;
  }

  function dateLabel(parts, tickMarkType) {
    var month = MONTH_NAMES[parts.month - 1] || "";
    if (tickMarkType === YEAR) return String(parts.year);
    if (tickMarkType === MONTH) return month + " " + parts.year;
    return month + " " + parts.day;
  }

  /* The label for one tick. `time` is the bar's chart time: a unix-seconds number
   * for intraday bars, a date for calendar bars. */
  function tickLabel(time, tickMarkType, timeZone) {
    var parts = calendarParts(time);
    if (parts) return dateLabel(parts, tickMarkType);

    if (typeof time === "number") {
      var date = new Date(time * 1000);
      // A time tick on an intraday dataset is the candle's own period.
      if (tickMarkType === TIME_WITH_SECONDS) {
        return zoneLabel(timeZone, { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false }, "hms", date);
      }
      if (tickMarkType === TIME) {
        return zoneLabel(timeZone, { hour: "2-digit", minute: "2-digit", hour12: false }, "hm", date);
      }
      // Zoomed out far enough that the library wants a date: give it the date IN THE
      // MARKET'S ZONE, not the browser's, or the label can name the day either side
      // of the session it belongs to.
      if (tickMarkType === MONTH) {
        return zoneLabel(timeZone, { month: "short", year: "numeric" }, "my", date);
      }
      return zoneLabel(timeZone, { month: "short", day: "numeric" }, "md", date);
    }
    return String(time);
  }

  function tickMarkFormatterFor(timeZone) {
    return function (time, tickMarkType) {
      return tickLabel(time, tickMarkType, timeZone);
    };
  }

  /* "12:25 EDT" — the label a TABLE cell wants for a bar, as opposed to the axis.
   *
   * The same two corrections the axis needed, in a table's shorter form: the time is the
   * market's own (a 12:25 New York bar is not 16:25 anywhere the reader happens to sit), the
   * zone is named by its abbreviation rather than by an offset — "+00:00" says how the stamp is
   * STORED, which is nobody's question about a bar — and the seconds are dropped, because a bar
   * stamp is always on the minute and ":00" on every row is furniture.
   */
  function marketStamp(timeZone, date) {
    var bits = formatter(timeZone, {
      hour: "2-digit", minute: "2-digit", timeZoneName: "short",
    }, "market").formatToParts(date);
    var found = {};
    for (var i = 0; i < bits.length; i++) found[bits[i].type] = bits[i].value;
    return found.hour + ":" + found.minute + (found.timeZoneName ? " " + found.timeZoneName : "");
  }

  /* ``marketStamp`` for a bar the server handed over as an ISO stamp. Anything unparseable is
   * returned as it came: a bar whose stamp cannot be read is worth showing as-is, since the
   * thing it came from had a reason to write it that way. Empty is the dash every other empty
   * cell on the page uses — NOT the epoch, which ``new Date(null)`` quietly is. */
  function stampCell(value, timeZone) {
    if (value === null || value === undefined || value === "") return "—";
    var when = new Date(value);
    if (Number.isNaN(when.getTime())) return String(value);
    return marketStamp(timeZone || "UTC", when);
  }

  /* `timeScale` options for a chart of `interval` bars stamped in `timeZone`.
   *
   * `timeVisible` is the switch that lets the axis show a time of day at all: with it
   * off the library labels every intraday candle with the same date, which is the
   * behaviour being replaced. It stays off for calendar bars, where there is no time
   * of day to show. */
  function timeScaleOptions(interval, timeZone) {
    var intraday = isIntraday(interval);
    return {
      timeVisible: intraday,
      secondsVisible: false,
      // Applied for calendar bars too: the same date formatting either way, so a
      // daily axis and an intraday one cannot drift apart.
      tickMarkFormatter: tickMarkFormatterFor(timeZone),
    };
  }

  /* `localization` options: the CROSSHAIR's time label, which the library also
   * formats itself (also in UTC). */
  function localizationOptions(timeZone) {
    return {
      locale: "en-US",
      timeFormatter: function (time) {
        var parts = calendarParts(time);
        if (parts) {
          return parts.year + "-" + pad(parts.month) + "-" + pad(parts.day);
        }
        if (typeof time === "number") return stampLabel(timeZone, new Date(time * 1000));
        return String(time);
      },
    };
  }

  global.ChartTime = {
    isIntraday: isIntraday,
    tickLabel: tickLabel,
    marketStamp: marketStamp,
    stampCell: stampCell,
    timeScaleOptions: timeScaleOptions,
    localizationOptions: localizationOptions,
    CALENDAR_BARS: CALENDAR_BARS,
  };
})(typeof window !== "undefined" ? window : globalThis);

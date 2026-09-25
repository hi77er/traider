/* The volume read-out: the amount of the bar under the crosshair, over that bar's own column.
 *
 * Shared by the Strategy lab's main chart and the Session monitor's session chart — both draw
 * volume as a histogram in a band at the bottom of the price pane, and both want the number behind
 * the bar they are pointing at rather than a height to compare by eye.
 *
 * Two things this module refuses to guess:
 *
 * * **The number is ABSOLUTE.** It is written by hand — ``58,460`` — rather than with
 *   ``toLocaleString``, so the same amount reads the same way on any machine, and rather than
 *   with the library's ``volume`` price format, which prints a compacted "58.46K" on the scale.
 *   A bar's own value is not a scale label.
 * * **The caller says WHICH series.** A relative-volume bar is a ratio; a label that printed one
 *   as an amount would be a lie the page told by itself, so nothing is read implicitly.
 *
 * The label element is a SIBLING of the chart host, positioned in a frame around it: the library
 * draws its canvases INTO the host and takes back whatever else is put there.
 */
(function (global) {
  "use strict";

  const GROUPS = /\B(?=(\d{3})+(?!\d))/g;

  /* ``58460`` -> ``58,460``. Kept separate from the wiring so a test can name a value without a
   * chart, and so both pages write an amount the same way. */
  function format(value) {
    if (value === null || value === undefined || value === "") return "";
    const n = Number(value);
    return Number.isFinite(n) ? String(Math.round(n)).replace(GROUPS, ",") : "";
  }

  /* One leave listener per host element. ``attach`` runs again every time a page rebuilds its
   * chart — a day change, a dataset reload — while the host element outlives all of them, so the
   * listener is registered once and calls whichever controller is current. */
  const hosts = new WeakMap();

  /* Follow ``chart``'s crosshair and label the bar of ``series``, positioning the label against
   * ``host`` (the element the chart was built in). Returns a controller — ``hide`` puts the label
   * away, which a page does when the series stops being drawn. Returns null when anything it
   * needs is missing: a page whose chart library did not load must not throw here. */
  function attach(options) {
    const opts = options || {};
    const chart = opts.chart;
    const series = opts.series;
    const host = opts.host;
    const label = opts.label;
    if (!chart || !series || !host || !label) return null;

    function hide() {
      label.hidden = true;
    }

    function show(param) {
      const bar = param && param.time !== undefined && param.seriesData
        ? param.seriesData.get(series) : null;
      const value = bar ? bar.value : null;
      const top = value === null || value === undefined
        ? null : series.priceToCoordinate(Number(value));
      // Nothing under the pointer — a gap in the session, the whitespace past the last bar, the
      // time axis, or the pointer off the pane. The amount read last has to go, or a stale number
      // sits over a bar nobody is pointing at.
      if (!param || !param.point || top === null || top === undefined) { hide(); return; }
      label.textContent = format(value);
      label.hidden = false;
      // Centred on the bar and lifted clear of it: the value's own y IS the top of that bar.
      // Nudged in at the edges, or half the label hangs over the price scale on the first and
      // last bars.
      const half = label.offsetWidth / 2;
      const width = host.clientWidth;
      const x = Math.min(Math.max(param.point.x, half + 2), Math.max(width - half - 2, half + 2));
      label.style.left = `${x}px`;
      label.style.top = `${top}px`;
    }

    let record = hosts.get(host);
    if (!record) {
      record = { hide: null };
      hosts.set(host, record);
      host.addEventListener("mouseleave", () => { if (record.hide) record.hide(); });
    }
    record.hide = hide;

    chart.subscribeCrosshairMove(show);
    hide(); // nothing is read out until the pointer is over a bar
    return { show: show, hide: hide };
  }

  global.ChartVolume = { attach: attach, format: format };
})(typeof window !== "undefined" ? window : globalThis);

/* Pinch-zoom for a lightweight-charts chart, clamped to THAT chart's data.
 *
 * One implementation for every chart in the app — the dashboard's price chart,
 * every indicator pane it draws separately (MACD, RSI, ATR, momentum, volatility,
 * relative volume, absolute volume), the backtest equity curve, and the report
 * page's equity/drawdown charts — because "where the zoom stops" is a property of
 * the chart and its data, not of the series it happens to hold.
 *
 * Why it is not just the library's own zoom:
 *
 *  - A plain mouse wheel must never zoom. The library treats ANY vertical wheel
 *    delta as zoom, so a two-finger swipe that carries even a small vertical
 *    component zooms while the user means to pan. Only a trackpad/touch PINCH
 *    zooms here, which the browser reports as a wheel event with `ctrlKey` set.
 *  - The zoom must stop at the data on the wide end. One logical unit IS one bar,
 *    so a chart's series occupies [0, barCount): asking for a window wider than that
 *    is asking for something the chart cannot draw, and the library clamps what it
 *    reports back. Re-deriving the next step from ITS answer made the view
 *    collapse to a handful of bars the moment a pinch-out went past the end of the
 *    data ("the zoom restarts at maximum zoom in"). Zooming out therefore stops once
 *    the window is as wide as the whole series, and zooming in stops at a single bar
 *    — the library leaves maxBarSpacing unlimited, so nothing else bounds that end.
 *  - The zoom must NOT move the window. It only changes how much is shown, around
 *    the bar under the pointer; the position the user panned to is theirs, including
 *    the empty space before the first bar or after the last one (dragging the chart
 *    so its tail sits mid-screen and then pinching used to reset the view to fill
 *    the width first, because the window was snapped back inside the data).
 *
 * Callers keep the library's own wheel zoom OFF (`handleScale.mouseWheel: false`)
 * and leave panning to it (`handleScroll.mouseWheel: true`), then bind each chart's
 * element here. `bind` is idempotent per element and re-points the resolver, so an
 * element that outlives its chart (a rebuild) stays correct.
 *
 * Assigned to `window` rather than declared with `const`: this file is a shared
 * asset loaded by two different pages (the dashboard and the report), and a global
 * is what makes that contract visible — the same way `LightweightCharts` arrives.
 */
window.ChartZoom = (function () {
  const resolvers = new WeakMap(); // element -> () => {chart, barCount}
  const bound = new WeakSet(); // elements that already carry the listener

  // One pinch step, clamped to the data. Exported for the behavioural tests.
  function clampRange(range, deltaY, ratio, barCount) {
    const span = range.to - range.from;
    if (!(span > 0)) return null;
    const anchor = range.from + ratio * span; // the bar under the pointer stays put
    const factor = Math.exp(-deltaY * 0.005); // pinch out (negative delta) -> zoom in
    // A chart whose bar count is unknown (not yet data-fed) keeps the old
    // proportional bound rather than locking the view to nothing.
    const maxSpan = barCount > 1 ? barCount : span * 10;
    const newSpan = Math.min(maxSpan, Math.max(1, span / factor));
    // The window keeps its POSITION: the bar under the pointer stays under it and
    // nothing else moves. Deliberately no "snap back inside the data" here — a
    // chart panned into the empty space that does not exist before the first bar
    // or after the last one zooms from where the user left it. Pulling the window
    // back into the data made a pinch after such a pan reset the chart to fill the
    // whole width first and only then zoom.
    const newFrom = anchor - (anchor - range.from) * (newSpan / span);
    return { from: newFrom, to: newFrom + newSpan };
  }

  function bind(el, resolve) {
    if (!el || typeof resolve !== "function") return;
    resolvers.set(el, resolve);
    if (bound.has(el)) return; // one listener per element — the resolver was replaced
    bound.add(el);
    el.addEventListener("wheel", (ev) => {
      if (!ev.ctrlKey) return; // plain wheel never zooms — the library pans instead
      ev.preventDefault(); // stop the browser from zooming the whole page instead
      const spec = (resolvers.get(el) || (() => null))();
      if (!spec || !spec.chart) return;
      const scale = spec.chart.timeScale();
      const range = scale.getVisibleLogicalRange();
      if (!range) return;
      const box = el.getBoundingClientRect();
      if (!box.width) return;
      const ratio = Math.max(0, Math.min(1, (ev.clientX - box.left) / box.width));
      const next = clampRange(range, ev.deltaY, ratio, spec.barCount | 0);
      if (!next) return;
      // At either limit the next step equals the range already shown: applying it
      // again would only make the library re-render the same view on every wheel
      // tick of a gesture that has nothing left to do.
      if (Math.abs(next.from - range.from) < 1e-9 && Math.abs(next.to - range.to) < 1e-9) {
        return;
      }
      scale.setVisibleLogicalRange(next);
    }, { passive: false });
  }

  return { bind: bind, clampRange: clampRange };
})();

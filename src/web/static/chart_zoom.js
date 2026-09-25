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
 *  - A wheel event that arrives while the pointer is DOWN is part of a drag, not a gesture of its
 *    own. On a trackpad the same two fingers that move the chart also report as a scroll/pinch, so
 *    a drag used to ZOOM while the reader was only moving the chart sideways. Those events are
 *    swallowed whole: the pointer is already doing the moving.
 *  - A gesture that is mostly SIDEWAYS is a move, not a zoom. Two fingers travelling together is a
 *    pan, and the OS reports that as a magnify (ctrl) often enough that zooming on it is how a
 *    reader trying to reach an earlier stretch of the session ended up at maximum zoom instead.
 *  - No single event may take the view anywhere drastic. A pinch arrives as a stream
 *    of wheel events, so the step per event is capped; unbounded, one flick collapsed
 *    the window onto a single bar in one frame.
 *
 * Callers keep the library's own wheel zoom OFF (`handleScale.mouseWheel: false`, and `pinch:
 * false` as a second line of defence) and leave PANNING to it (`handleScroll.mouseWheel: true`),
 * then bind each chart's element here. Both are needed and neither is sufficient: the library
 * scales ANY ctrl+wheel whatever those options say (measured on the session chart: a pure
 * sideways one still zoomed 1.6x with `pinch: false`), which is why this module's listener runs in
 * the CAPTURE phase and STOPS the event — the library's own listener on the canvas never sees a
 * pinch, so there is exactly one zoomer and it is the bounded one below. `bind` is idempotent per
 * element and re-points the resolver, so an element that outlives its chart (a rebuild) stays
 * correct.
 *
 * Assigned to `window` rather than declared with `const`: this file is a shared
 * asset loaded by three pages (the Strategy lab, the Session monitor and the
 * report), and a global is what makes that contract visible — the same way
 * `LightweightCharts` arrives.
 */
window.ChartZoom = (function () {
  // Pixels per bar at maximum zoom-out. Every chart here passes this as its
  // ``minBarSpacing``, and it has to be small: the library will not draw more bars
  // than fit at ITS ``minBarSpacing`` (0.5 by default), silently applying a NARROWER
  // range than the one it was given. A 528px-wide plot therefore refused anything
  // past ~1,056 bars, so 60 days of 5-minute candles (3,191 bars) could only ever be
  // zoomed out to about a fortnight — even though the clamp below allows the whole
  // series, and even though asking for the whole series is exactly what "zoom out as
  // far as it goes" means. Nothing was wrong with the clamp; the two limits simply
  // disagreed, and the library's was the smaller one.
  //
  // 0.01 leaves room for far more bars than a 60-day window can hold at any bar size
  // (60 days of 1-minute RTH bars is ~23,400). It only ever bites at extreme
  // zoom-out, where the candles are sub-pixel anyway.
  const MIN_BAR_SPACING = 0.01;

  // The most one wheel event may change the zoom, as a log-step: e^0.35 ≈ 1.42x. A trackpad
  // sends a pinch as a stream of events, so a gesture still zooms the whole way in a fraction
  // of a second; what this stops is a SINGLE event taking the view somewhere drastic.
  // Exported with the clamp, for the same reason: a test has to be able to name the bound.
  const MAX_ZOOM_STEP = 0.35;

  const resolvers = new WeakMap(); // element -> () => {chart, barCount}
  const bound = new WeakSet(); // elements that already carry the listener

  // One pinch step, clamped to the data. Exported for the behavioural tests.
  function clampRange(range, deltaY, ratio, barCount) {
    const span = range.to - range.from;
    if (!(span > 0)) return null;
    const anchor = range.from + ratio * span; // the bar under the pointer stays put
    // ONE STEP AT A TIME. A trackpad reports a pinch as a STREAM of wheel events, so a gesture
    // still zooms smoothly and all the way — but no single event may take the view somewhere
    // drastic. Unbounded, one event (a fast flick, a coarse wheel, the tail of a gesture, each
    // reported with a delta in the hundreds) collapsed the window onto a SINGLE bar in one frame:
    // the reader saw the chart "suddenly zoom in to the maximum" and lost the stretch they were
    // looking at. Measured on the session chart: deltaY of -400 went from 72 bars to 9.
    const step = Math.max(-MAX_ZOOM_STEP, Math.min(MAX_ZOOM_STEP, -deltaY * 0.005));
    const factor = Math.exp(step); // pinch out (negative delta) -> zoom in
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

  /* Move the window sideways by a wheel gesture.
   *
   * The library pans a wheel itself, and for a plain one that is left to it. A PINCH is not left to
   * it (see ``bind``), so a pinch that is mostly sideways — two fingers travelling together, which
   * a trackpad reports with ctrl set as well — has to be moved here, by the same arithmetic the
   * library uses: the pixels travelled, as a share of the pane, in bars. */
  function panBy(scale, deltaX, paneWidth) {
    const range = scale.getVisibleLogicalRange();
    if (!range || !(paneWidth > 0)) return;
    const span = range.to - range.from;
    if (!(span > 0)) return;
    const shift = (deltaX / paneWidth) * span;
    scale.setVisibleLogicalRange({ from: range.from + shift, to: range.to + shift });
  }

  function bind(el, resolve) {
    if (!el || typeof resolve !== "function") return;
    resolvers.set(el, resolve);
    if (bound.has(el)) return; // one wheel listener per element — the resolver was replaced
    bound.add(el);

    // Is the pointer DOWN on this chart? A drag is how the reader moves the chart, and on a
    // trackpad the same two fingers also report as a scroll/pinch — so a wheel event that arrives
    // mid-drag is an artefact of the move, not a gesture of its own. This is the case that made a
    // drag ZOOM instead of move. Tracked on the window for the release, so a drag that ends
    // outside the element cannot leave the flag stuck on.
    let dragging = false;
    el.addEventListener("mousedown", () => { dragging = true; }, true);
    window.addEventListener("mouseup", () => { dragging = false; });
    window.addEventListener("blur", () => { dragging = false; });

    el.addEventListener("wheel", (ev) => {
      if (dragging || ev.buttons) {
        // Part of a drag. Swallowed WHOLE — no zoom, and no pan either, because the pointer is
        // already moving the chart: two sources moving it at once is a chart that fights the hand.
        ev.preventDefault();
        ev.stopPropagation();
        return;
      }
      if (!ev.ctrlKey) return; // a plain wheel never zooms — the library pans it
      // From here it is a PINCH, and this module is the only thing allowed to act on one. The
      // library scales ANY ctrl+wheel — sideways or not, and whatever `handleScale` says: measured
      // on this chart, a PURE sideways one (dx -200, dy 0) still zoomed 1.6x with `pinch: false`.
      // So the event is taken in the CAPTURE phase, before the library's own listener on the
      // canvas, and stopped there: both the page and the library are kept out of it.
      ev.preventDefault(); // or the browser zooms the whole PAGE instead
      ev.stopPropagation();

      const spec = (resolvers.get(el) || (() => null))();
      if (!spec || !spec.chart) return;
      const scale = spec.chart.timeScale();
      const box = el.getBoundingClientRect();
      if (!box.width) return;

      // A gesture that is mostly SIDEWAYS is a MOVE, not a zoom: two fingers travelling together
      // is a pan, and the OS reports that as a magnify (ctrl) often enough that zooming on it is
      // how a reader trying to reach an earlier stretch of the session ended up at maximum zoom.
      if (Math.abs(ev.deltaX) > Math.abs(ev.deltaY)) {
        const width = typeof scale.width === "function" ? scale.width() : 0;
        panBy(scale, ev.deltaX, width || box.width);
        return;
      }

      const range = scale.getVisibleLogicalRange();
      if (!range) return;
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
    }, { passive: false, capture: true });
  }

  return { bind: bind, clampRange: clampRange, MIN_BAR_SPACING: MIN_BAR_SPACING,
           MAX_ZOOM_STEP: MAX_ZOOM_STEP };
})();

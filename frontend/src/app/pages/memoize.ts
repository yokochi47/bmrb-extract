/** Memoize a pure `source → value` builder on the source object's identity.
 *
 * The per-saveframe chart-panel builders are called from the templates, so they
 * run on every change-detection pass. Uncached, each call returns freshly built
 * `option` objects; the new identity re-triggers `app-echart`'s render effect and
 * forces a full `setOption(option, true)` redraw of every chart on the page.
 *
 * The saveframes come straight from the `nmr_preview` / statistics signals, so
 * their identity is stable for as long as the response is. Keying a `WeakMap` on
 * them therefore caches for exactly that lifetime: a new response brings new
 * saveframe objects (a cache miss, so the panels are rebuilt) and lets the stale
 * entries be collected. */
export function memoizeBySource<S extends object, V>(build: (source: S) => V): (source: S) => V {
  const cache = new WeakMap<S, V>();
  return (source: S): V => {
    if (cache.has(source)) return cache.get(source) as V;
    const value = build(source);
    cache.set(source, value);
    return value;
  };
}

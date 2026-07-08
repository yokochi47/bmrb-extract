import {
  Component,
  ElementRef,
  OnDestroy,
  computed,
  effect,
  input,
  signal,
  viewChild,
} from '@angular/core';

/** Minimal shapes of the lazily-loaded ECharts API we use (avoids pulling the
 * full echarts types into this module / the initial bundle). */
interface EChartInstance {
  setOption(option: object, notMerge?: boolean): void;
  resize(): void;
  dispose(): void;
}
interface EChartsCore {
  use(modules: unknown[]): void;
  init(el: HTMLElement): EChartInstance;
}

/** Load + register ECharts once (tree-shaken: only the chart/component types we
 * need). Dynamic import keeps ECharts in a lazy chunk, off the initial bundle. */
let echartsPromise: Promise<EChartsCore> | null = null;
function loadEcharts(): Promise<EChartsCore> {
  if (!echartsPromise) {
    echartsPromise = (async () => {
      const core = (await import('echarts/core')) as unknown as EChartsCore;
      const charts = await import('echarts/charts');
      const components = await import('echarts/components');
      const renderers = await import('echarts/renderers');
      core.use([
        charts.BarChart,
        charts.ScatterChart,
        charts.CustomChart,
        // LineChart is required by the per-residue plots (RCI/S², NMR RMSD,
        // dihedral / RDC per residue); without it the axes render but no line.
        charts.LineChart,
        components.GridComponent,
        components.TooltipComponent,
        components.LegendComponent,
        components.TitleComponent,
        // markArea (secondary-structure bands) / markLine (thresholds) used by
        // the per-residue line charts.
        components.MarkAreaComponent,
        components.MarkLineComponent,
        renderers.CanvasRenderer,
      ]);
      return core;
    })();
  }
  return echartsPromise;
}

/** Reusable ECharts host: renders the bound `option` into a sized div, resizes
 * with its container, and disposes on destroy. With an `aspect` set the chart is
 * sized proportionally to its data extent (kept centered); otherwise it fills
 * the width at a fixed height. */
@Component({
  selector: 'app-echart',
  standalone: true,
  template: `<div
    #host
    class="mx-auto w-full"
    [style.max-width.px]="maxWidth()"
    [style.height.px]="boxHeight()"
  ></div>`,
})
export class EchartComponent implements OnDestroy {
  option = input<object | null>(null);
  /** Base / minimum chart height (px). */
  height = input(400);
  /** Desired box aspect ratio (height / width). When set the chart keeps this
   * ratio — full width until the height would exceed `maxHeight`, then capped
   * via max-width — and never shorter than `height` (a too-short result, e.g.
   * few y data points, falls back to the base height at full width). Null → fill
   * the width at the fixed `height`. */
  aspect = input<number | null>(null);
  /** Upper bound on the proportional height (px). */
  maxHeight = input(700);
  /** Horizontal / vertical chart chrome in px (grid margins + legend). Subtracted
   * so `aspect` applies to the plot area, not the whole box — e.g. a square
   * contact map stays square even with a wide right-side legend. */
  marginX = input(0);
  marginY = input(0);

  private host = viewChild<ElementRef<HTMLDivElement>>('host');
  private chart: EChartInstance | null = null;
  private resizeObs?: ResizeObserver;
  /** Measured content width of the host element. */
  private hostWidth = signal(0);

  /** Max width that keeps the aspect ratio within `maxHeight` (null → unbounded,
   * so the chart fills its container). */
  maxWidth = computed<number | null>(() => {
    const a = this.aspect();
    return a ? Math.round((this.maxHeight() - this.marginY()) / a + this.marginX()) : null;
  });

  /** Height (px) so the plot area (width − marginX) honours the aspect ratio,
   * clamped to [height, maxHeight]. */
  boxHeight = computed<number>(() => {
    const a = this.aspect();
    const w = this.hostWidth();
    if (!a || !w) return this.height();
    const boxH = a * Math.max(0, w - this.marginX()) + this.marginY();
    return Math.round(Math.min(this.maxHeight(), Math.max(this.height(), boxH)));
  });

  constructor() {
    effect(() => {
      const option = this.option();
      const host = this.host()?.nativeElement;
      if (!option || !host) return;
      void this.render(host, option);
    });
    // Re-fit when the computed height changes (width changes are already caught
    // by the ResizeObserver, which also re-runs this via hostWidth).
    effect(() => {
      this.boxHeight();
      this.chart?.resize();
    });
  }

  private async render(host: HTMLElement, option: object): Promise<void> {
    const echarts = await loadEcharts();
    if (!this.chart) {
      this.chart = echarts.init(host);
      this.resizeObs = new ResizeObserver((entries) => {
        this.hostWidth.set(entries[0]?.contentRect.width ?? 0);
        this.chart?.resize();
      });
      this.resizeObs.observe(host);
    }
    this.chart.setOption(option, true);
  }

  ngOnDestroy(): void {
    this.resizeObs?.disconnect();
    this.chart?.dispose();
    this.chart = null;
  }
}

import { Component, ElementRef, OnDestroy, effect, input, viewChild } from '@angular/core';

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
        components.GridComponent,
        components.TooltipComponent,
        components.LegendComponent,
        components.TitleComponent,
        renderers.CanvasRenderer,
      ]);
      return core;
    })();
  }
  return echartsPromise;
}

/** Reusable ECharts host: renders the bound `option` into a sized div, resizes
 * with its container, and disposes on destroy. */
@Component({
  selector: 'app-echart',
  standalone: true,
  template: `<div #host class="w-full" [style.height.px]="height()"></div>`,
})
export class EchartComponent implements OnDestroy {
  option = input<object | null>(null);
  height = input(400);

  private host = viewChild<ElementRef<HTMLDivElement>>('host');
  private chart: EChartInstance | null = null;
  private resizeObs?: ResizeObserver;

  constructor() {
    effect(() => {
      const option = this.option();
      const host = this.host()?.nativeElement;
      if (!option || !host) return;
      void this.render(host, option);
    });
  }

  private async render(host: HTMLElement, option: object): Promise<void> {
    const echarts = await loadEcharts();
    if (!this.chart) {
      this.chart = echarts.init(host);
      this.resizeObs = new ResizeObserver(() => this.chart?.resize());
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

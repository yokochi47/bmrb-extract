#!/usr/bin/env python3
"""Render the static "bmrb_extract data conversion statistical report" PDF.

Data source: the last NMR data-processing JSON report (the convert_nmr_data
workflow's *-str_deposit.json). Provenance extras (input files, target
deposition system, processed site) come from a small provenance.json the flow
assembles from the DB — this container never touches the database.

Pipeline (Python is the single data-shaping layer):
  1. Extract output_statistics + ensemble_composition from the report JSON.
  2. Build chart_inputs.json (positional args for the shared chart builders).
  3. Shell out to `node render_charts.mjs` -> per-chart SVGs + charts.json.
  4. Render report.html (Jinja2) with the SVGs inlined; WeasyPrint -> PDF (A4).

Usage:
  generate_report.py --report <deposit.json> --out <C_<id>_report.pdf>
                     [--provenance <provenance.json>] [--work-dir <dir>]
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = APP_DIR / 'templates'
ASSETS_DIR = APP_DIR / 'assets'
RENDERER = APP_DIR / 'render_charts.mjs'

# Preferred category order for the distance mean/all-violation histograms
# (mirrors DIST_CAT_ORDER in report-charts.ts; passed as the `order` arg).
DIST_CAT_ORDER = [
    'intra-residue', 'sequential', 'medium_range', 'long_range', 'inter-chain',
    'hydrogen_bond', 'disulfide_bond', 'diselenide_bond', 'metal_coordiantion',
]
# Fixed distance sub-type categories (key/label/color) shared by the per-model
# and per-ensemble distance charts (mirrors the frontend computeds).
DIST_CATS = [
    {'key': 'ir_viol_count', 'label': 'Intra-residue', 'color': '#5470c6'},
    {'key': 'sq_viol_count', 'label': 'Sequential', 'color': '#a3c4f3'},
    {'key': 'mr_viol_count', 'label': 'Medium range', 'color': '#3ba272'},
    {'key': 'lr_viol_count', 'label': 'Long range', 'color': '#c0ca33'},
    {'key': 'ic_viol_count', 'label': 'Inter-chain', 'color': '#808000'},
]

TARGET_DEPSYS_LABEL = {
    'onedep': 'OneDep (new deposition)',
    'repl_cs': 'OneDep (ongoing deposition - replacing assigned chemical shifts)',
    'bmrbdep': 'BMRBdep (new deposition)',
}


# ------------------------------------------------------------- extraction --- #

def _format_size(b):
    """Human-readable byte size (mirrors the frontend formatSize: 1024 divisor,
    KB/MB/GB labels, one decimal). None → None (row skipped); 0 → '0 B'."""
    if b is None:
        return None
    if not b:
        return '0 B'
    import math
    units = ['B', 'KB', 'MB', 'GB']
    i = min(int(math.floor(math.log(b) / math.log(1024))), len(units) - 1)
    return f'{b / (1024 ** i):.1f} {units[i]}'


def restraint_type_label(value) -> str:
    """Display label for a violation-summary restraint_type (mirrors the TS
    restraintTypeLabel): a "<abbr>; <sub-type>" becomes an indented lower-case
    sub-type; a top-level type is capitalized with underscores as spaces."""
    if not value:
        return ''
    value = str(value)
    semi = value.find(';')
    if semi >= 0:
        return '  ' + value[semi + 1:].replace('_', ' ').lstrip()
    s = value.replace('_', ' ').lower()
    return s[:1].upper() + s[1:]


def output_statistics(report: dict) -> dict:
    return (report.get('information', {}) or {}).get('output_statistics', {}) or {}


def ensemble_composition(report: dict) -> dict:
    """From the pdbx input source (mirrors the backend _ensemble_composition)."""
    for src in (report.get('information', {}) or {}).get('input_sources', []) or []:
        if isinstance(src, dict) and src.get('file_type') == 'pdbx':
            return src.get('ensemble_composition', {}) or {}
    return {}


def report_timestamp_utc(report_path: Path) -> str:
    """The report file's mtime in UTC (matches /api/output_statistics)."""
    ts = datetime.fromtimestamp(report_path.stat().st_mtime, tz=timezone.utc)
    return ts.strftime('%Y-%m-%d %H:%M:%S')


# ------------------------------------------------------------ chart specs --- #

def _dihed_ensemble_cats(rows: list) -> list:
    """Dynamic dihedral-angle categories (phi, psi, then others sorted); the
    aggregate 'total' column is kept out of the per-ensemble chart."""
    seen = set()
    for r in rows:
        for k in (r or {}):
            if k.endswith('_viol_count'):
                seen.add(k)
    fixed = ['phi_viol_count', 'psi_viol_count', 'total_viol_count']
    others = sorted(k for k in seen if k not in fixed)
    ordered = ([k for k in ('phi_viol_count', 'psi_viol_count') if k in seen] + others)
    return [{'key': k, 'label': k[:-len('_viol_count')].capitalize()} for k in ordered]


def _value_pts(rows: list, value_key: str, cat_key: str) -> list:
    """Shape {value, cat} points for stackedValueHistogram."""
    pts = []
    for r in rows or []:
        v = r.get(value_key)
        c = r.get(cat_key)
        if isinstance(v, (int, float)) and c:
            pts.append({'value': v, 'cat': str(c)})
    return pts


def build_chart_inputs(stats: dict, ensemble: dict) -> list:
    """Positional-arg specs for the shared builders (see render_charts.mjs).
    Charts with no data resolve to null in charts.json and are simply omitted."""
    rs = stats.get('restraint_summary', {}) or {}
    specs = []

    # 1. Ensemble PCA scatter.
    clusters = ensemble.get('cluster_analysis', []) or []
    specs.append({'id': 'ens_pca', 'builder': 'pcaChartOption',
                  'args': [clusters], 'width': 560, 'height': 460})

    # 7.1 / 8.1 distribution of restraints & violations.
    specs.append({'id': 'dist_violation', 'builder': 'distViolationChart',
                  'args': [rs.get('dist_violation_summary', [])], 'width': 680, 'height': 420})
    specs.append({'id': 'dihed_violation', 'builder': 'dihedViolationChart',
                  'args': [rs.get('dihed_violation_summary', [])], 'width': 680, 'height': 420})
    specs.append({'id': 'rdc_violation', 'builder': 'rdcViolationChart',
                  'args': [rs.get('rdc_violation_summary', [])], 'width': 680, 'height': 420})

    # 7.2 / 8.2 per-model violation statistics (dual-axis).
    specs.append({'id': 'dist_model', 'builder': 'modelViolationChartOption',
                  'args': [rs.get('dist_violation_for_each_model', []), 'Å', DIST_CATS],
                  'width': 700, 'height': 420})
    specs.append({'id': 'dihed_model', 'builder': 'modelViolationChartOption',
                  'args': [rs.get('dihed_violation_for_each_model', []), '°',
                           [{'key': 'phi_viol_count', 'label': 'Phi'},
                            {'key': 'psi_viol_count', 'label': 'Psi'}]],
                  'width': 700, 'height': 420})
    rdc_model_rows = rs.get('rdc_violation_for_each_model', []) or []
    specs.append({'id': 'rdc_model', 'builder': 'modelViolationChartOption',
                  'args': [rdc_model_rows, 'Hz',
                           [c for c in _rdc_table_cols(rdc_model_rows)
                            if c['key'] != 'total_viol_count']],
                  'width': 700, 'height': 420})

    # 7.3 / 8.3 per-ensemble violation statistics.
    specs.append({'id': 'dist_ensemble', 'builder': 'violationEnsembleStackChart',
                  'args': [rs.get('dist_violation_for_ensemble', []), DIST_CATS],
                  'width': 680, 'height': 420})
    dihed_ens_rows = rs.get('dihed_violation_for_ensemble', []) or []
    specs.append({'id': 'dihed_ensemble', 'builder': 'violationEnsembleStackChart',
                  'args': [dihed_ens_rows, _dihed_ensemble_cats(dihed_ens_rows)],
                  'width': 680, 'height': 420})
    rdc_ens_rows = rs.get('rdc_violation_for_ensemble', []) or []
    specs.append({'id': 'rdc_ensemble', 'builder': 'violationEnsembleStackChart',
                  'args': [rdc_ens_rows,
                           [c for c in _rdc_table_cols(rdc_ens_rows)
                            if c['key'] != 'total_viol_count']],
                  'width': 680, 'height': 420})

    # 7.4 / 8.4 mean-violation histograms.
    specs.append({'id': 'dist_mean_hist', 'builder': 'meanViolationHistogram',
                  'args': [rs.get('most_violated_dist_restraints', []), 'distance_type',
                           'Å', DIST_CAT_ORDER], 'width': 680, 'height': 420})
    specs.append({'id': 'dihed_mean_hist', 'builder': 'meanViolationHistogram',
                  'args': [rs.get('most_violated_dihed_restraints', []), 'dihedral_angle_name',
                           '°', ['phi', 'psi']], 'width': 680, 'height': 420})
    specs.append({'id': 'rdc_mean_hist', 'builder': 'meanViolationHistogram',
                  'args': [_rdc_most_violated(rs.get('most_violated_rdc_restraints')),
                           'distance_type', 'Hz', []], 'width': 680, 'height': 420})

    # 7.5 / 8.5 all-violation histograms.
    specs.append({'id': 'dist_all_hist', 'builder': 'stackedValueHistogram',
                  'args': [_value_pts(rs.get('all_dist_violations', []), 'violation', 'distance_type'),
                           'Å', DIST_CAT_ORDER, 'Violation'], 'width': 680, 'height': 420})
    specs.append({'id': 'dihed_all_hist', 'builder': 'stackedValueHistogram',
                  'args': [_value_pts(rs.get('all_dihed_violations', []), 'violation', 'dihedral_angle_name'),
                           '°', ['phi', 'psi'], 'Violation'], 'width': 680, 'height': 420})
    specs.append({'id': 'rdc_all_hist', 'builder': 'stackedValueHistogram',
                  'args': [_value_pts(_rdc_most_violated(rs.get('all_rdc_violations')),
                                      'violation', 'distance_type'),
                           'Hz', [], 'Violation'], 'width': 680, 'height': 420})

    return specs


def _rdc_correlation_plot(plot):
    """Normalize an RDC correlation_plot into {groups:[{name, points:[{x,y,
    seq_id}], errors:[[...]]}]} for rdcCorrelationChartOption (mirrors the backend
    _scatter_plot with trim_label=False — seq_id keeps the full RDC vector)."""
    if not isinstance(plot, dict) or not plot.get('values'):
        return None
    errors_by_key = plot.get('errors') or {}
    groups = []
    for key, vals in plot['values'].items():
        pts = [{'x': p[0], 'y': p[1], 'seq_id': p[2]} for p in vals if len(p) >= 3]
        if pts:
            groups.append({'name': key, 'points': pts, 'errors': errors_by_key.get(key) or []})
    return {'groups': groups} if groups else None


def _rdc_q_rows(plot):
    """Quality-score rows [{type, count, r2, cornilescu_q, clore_q}] from a
    correlation_plot's q_scores (mirrors the backend _rdc_q_scores)."""
    q_scores = plot.get('q_scores') if isinstance(plot, dict) else None
    if not isinstance(q_scores, dict) or not q_scores:
        return []
    values = plot.get('values') or {}
    rows = []
    for vtype, scores in q_scores.items():
        if not isinstance(scores, dict):
            continue
        vals = values.get(vtype)
        rows.append({
            'type': vtype,
            'count': len(vals) if isinstance(vals, list) else None,
            'r2': scores.get('r2'),
            'cornilescu_q': scores.get('cornilescu_q'),
            'clore_q': scores.get('clore_q'),
        })
    return rows


def build_rdc_correlation(report: dict) -> tuple[list, list]:
    """Observed-vs-calculated RDC correlation scatter charts + quality-score tables
    per RDC-restraint saveframe, from stats_of_exptl_data (mirrors the backend
    nmr_preview and the download page's section-9 preamble). Returns
    (chart_specs, [{sf_framecode, chart_id?, q_rows}])."""
    info = report.get('information', {}) or {}
    specs, correlations = [], []
    for src in info.get('input_sources') or []:
        if not isinstance(src, dict):
            continue
        stats = src.get('stats_of_exptl_data')
        if not isinstance(stats, dict):
            continue
        for st in stats.get('rdc_restraint') or []:
            if not isinstance(st, dict):
                continue
            plot = st.get('correlation_plot')
            scatter = _rdc_correlation_plot(plot)
            q_rows = _rdc_q_rows(plot)
            if not scatter and not q_rows:
                continue
            entry = {'sf_framecode': st.get('sf_framecode', ''), 'q_rows': q_rows}
            if scatter:
                # Fixed size (the SSR renderer has no aspect logic); width chosen
                # so the plot area — width minus the left axis + right legend — is
                # ~square, keeping the y=x diagonal near 45°.
                cid = f'rdc_corr_{len(specs)}'
                specs.append({'id': cid, 'builder': 'rdcCorrelationChartOption',
                              'args': [scatter], 'width': 600, 'height': 480})
                entry['chart_id'] = cid
            correlations.append(entry)
    return specs, correlations


def build_chem_shift_charts(stats: dict) -> tuple[list, dict]:
    """Section 5 charts: per chemical-shift saveframe, the Z-score histogram(s)
    and the RCI/S² + NMR-RMSD per-residue line plots. Returns (chart specs,
    {list_id: [{id, title}]}) — the latter tells the template which charts belong
    under each saveframe. Uses report_data (ported from the backend) to shape the
    inputs, then the shared histogramOption / lineOption builders render them."""
    import report_data as rd

    specs: list = []
    by_sf: dict = {}
    for st in stats.get('chem_shift', []) or []:
        lid = st.get('list_id')
        entries = []
        for i, h in enumerate(rd.histogram_chart([st], inverse=True)):
            cid = f'cs{lid}_hist{i}'
            specs.append({'id': cid, 'builder': 'histogramOption',
                          'args': [h, 'Z-score', 'Number of chemical shifts',
                                   {'inverse': True, 'rangeLabels': True}],
                          'width': 680, 'height': 420})
            entries.append({'id': cid, 'kind': 'hist',
                            'title': 'Normalized assigned chemical shifts (Z-score)'})
        for i, c in enumerate(rd.rci_charts([st], auth=True)):
            cid = f'cs{lid}_rci{i}'
            # staticMode: drop per-point symbols + the non-interactive legend in the PDF.
            y_name = 'RCI / S² values' if str(c.get('label', '')).startswith('RCI') else 'NMR RMSD (Å)'
            specs.append({'id': cid, 'builder': 'lineOption', 'args': [c, y_name, {'staticMode': True}],
                          'width': 720, 'height': 360})
            kind = 'rci' if str(c.get('label', '')).startswith('RCI') else 'nmr'
            entries.append({'id': cid, 'kind': kind,
                            'title': f"{c.get('label')} — Auth_asym_ID: {c.get('chain')}"})
        by_sf[lid] = entries
    return specs, by_sf


_DIST_TABLE_COLS = [
    {'key': 'ir_viol_count', 'label': 'IR'}, {'key': 'sq_viol_count', 'label': 'SQ'},
    {'key': 'mr_viol_count', 'label': 'MR'}, {'key': 'lr_viol_count', 'label': 'LR'},
    {'key': 'ic_viol_count', 'label': 'IC'}, {'key': 'total_viol_count', 'label': 'Total'},
]
_TABLE_ROW_CAP = 250

# 6.1 restraint-count table (mirrors RESTRAINT_KEY_ORDER / RESTRAINT_LABEL_HTML /
# DIST_TYPE_LABELS and restraintProps in page.download.ts).
_RESTRAINT_KEY_ORDER = [
    'total_distance_restraints', 'intra-residue', 'sequential', 'medium_range',
    'long_range', 'inter-chain', 'hydrogen_bond_restraints', 'disulfide_bond_restraints',
    'diselenide_bond_restraints', 'metal_coordination_restraints',
    'total_dihedral_angle_restraints', 'total_rdc_restraints', 'number_of_unmapped_restraints',
    'number_of_restraints_per_residue', 'number_of_long_range_restraints_per_residue',
]
_RESTRAINT_LABEL_HTML = {
    'intra-residue': 'Intra-residue (<em>| i - j | = 0</em>)',
    'sequential': 'Sequential (<em>| i - j | = 1</em>)',
    'medium_range': 'Medium range (<em>1 &lt; | i - j | &lt; 5</em>)',
    'long_range': 'Long range (<em>| i - j | ≥ 5</em>)',
    # Preserve the RDC acronym (the capitalize fallback would lowercase it).
    'total_rdc_restraints': 'Total RDC restraints',
}
_DIST_TYPE_LABELS = {
    'ir': 'Intra-residue', 'se': 'Sequential', 'mr': 'Medium range',
    'lr': 'Long range', 'ic': 'Inter-chain',
}
_BOND_RESTRAINT_RE = re.compile(
    r'^(hydrogen_bond|disulfide_bond|diselenide_bond|metal_coordination)_restraints$')


def _dist_type_subclasses(csv):
    return ', '.join(_DIST_TYPE_LABELS.get(c, c) for c in
                     (p.strip() for p in str(csv).split(',')) if c)


def _restraint_props(rs):
    """6.1 rows [{label(HTML), value}]: scalar restraint counts, ordered, with the
    *_bond / metal_coordination subclass list folded into the value as
    '{count} (Medium range, Long range)'. Mirrors restraintProps."""
    def rank(k):
        return _RESTRAINT_KEY_ORDER.index(k) if k in _RESTRAINT_KEY_ORDER else len(_RESTRAINT_KEY_ORDER)

    items = [
        (k, v) for k, v in rs.items()
        if not re.search('average|violation', k, re.I)
        and not k.endswith('_dist_types')
        and isinstance(v, (int, float, str)) and not isinstance(v, bool)
    ]
    rows = []
    for k, v in sorted(items, key=lambda kv: rank(kv[0])):
        m = _BOND_RESTRAINT_RE.match(k)
        types = rs.get(f'{m.group(1)}_dist_types') if m else None
        subs = _dist_type_subclasses(types) if isinstance(types, str) else ''
        label = _RESTRAINT_LABEL_HTML.get(k) or (k.replace('_', ' ').capitalize())
        rows.append({'label': label, 'value': f'{v} ({subs})' if subs else str(v)})
    return rows


_BOOKKEEPING_DEFS = [
    ('dist_restraint', '6.3', 'Bookkeeping of distance restraints',
     'There are no distance restraints.', 'distance restraints'),
    ('dihed_restraint', '6.4', 'Bookkeeping of dihedral-angle restraints',
     'There are no dihedral-angle restraints.', 'dihedral-angle restraints'),
    ('rdc_restraint', '6.5', 'Bookkeeping of RDC restraints',
     'There are no RDC restraints.', 'RDC restraints'),
    ('spectral_peak', '6.6', 'Bookkeeping of spectral peak lists',
     'There are no spectral peak lists.', 'spectral peaks'),
]


def _dihed_table_cols(rows):
    """Dynamic dihedral-angle columns: phi, psi, then other types sorted, total
    last (mirrors dihedModelViolations / dihedEnsembleViolations)."""
    seen = set()
    for r in rows or []:
        for k in (r or {}):
            if k.endswith('_viol_count'):
                seen.add(k)
    fixed = ['phi_viol_count', 'psi_viol_count', 'total_viol_count']
    others = sorted(k for k in seen if k not in fixed)
    ordered = ([k for k in ('phi_viol_count', 'psi_viol_count') if k in seen]
               + others + (['total_viol_count'] if 'total_viol_count' in seen else []))
    return [{'key': k, 'label': k[:-len('_viol_count')].capitalize()} for k in ordered]


def _rdc_table_cols(rows):
    """Dynamic RDC vector-type columns: types sorted, total last (mirrors
    rdcModelViolations). Labels use restraint_type_label (e.g. 'Rdc other')."""
    seen = set()
    for r in rows or []:
        for k in (r or {}):
            if k.endswith('_viol_count'):
                seen.add(k)
    others = sorted(k for k in seen if k != 'total_viol_count')
    ordered = others + (['total_viol_count'] if 'total_viol_count' in seen else [])
    return [{'key': k, 'label': restraint_type_label(k[:-len('_viol_count')])} for k in ordered]


def _rdc_most_violated(rows):
    """Normalise RDC most-violated / all-violation rows: the shared schema's only
    type slots are distance_type / dihedral_angle_name, so surface whichever the
    converter populated (the RDC vector type) in distance_type — the shared table
    (type_key='distance_type') and mean-violation histogram categorise by it."""
    out = []
    for r in rows or []:
        if not isinstance(r, dict):
            continue
        r = dict(r)
        r['distance_type'] = r.get('distance_type') or r.get('dihedral_angle_name')
        out.append(r)
    return out


def _viol_bins(rs, key):
    v = rs.get(key)
    return v if isinstance(v, list) else []


def _nonviolated(summary_rows):
    """Non-violated = restraint_count − viol_count, overall + per distance category."""
    def nv(r):
        return (r.get('restraint_count') or 0) - (r.get('viol_count') or 0) if r else 0

    def find(pred):
        return next((r for r in summary_rows if pred((r.get('restraint_type') or '').lower())), None)
    return {
        'total': nv(find(lambda t: t == 'total')),
        'ir': nv(find(lambda t: t.startswith('intra-residue'))),
        'sq': nv(find(lambda t: t.startswith('sequential'))),
        'mr': nv(find(lambda t: t.startswith('medium'))),
        'lr': nv(find(lambda t: t.startswith('long'))),
        'ic': nv(find(lambda t: t.startswith('inter-chain'))),
    }


def build_restraint_sections(stats: dict) -> dict:
    """Structured data for the NMR-restraint / distance / dihedral sections (6–8):
    per-model violation bins, restraint bookkeeping groups, and the per-model /
    per-ensemble / most-violated / all-violation tables. Mirrors the download
    page's restraint computeds."""
    import report_data as rd

    rs = stats.get('restraint_summary', {}) or {}
    dist_sum = rs.get('dist_violation_summary') or []
    dihed_sum = [r for r in (rs.get('dihed_violation_summary') or [])]
    rdc_sum = rs.get('rdc_violation_summary') or []

    def bookkeeping(key, noun):
        sfs = []
        for it in stats.get(key) or []:
            if not isinstance(it, dict):
                continue
            sfs.append({
                'list_id': it.get('list_id'),
                'title': f"{it.get('sf_framecode', '')} ({it.get('original_file_name', '')})".strip(),
                'mapped_to_model': it.get('number_of_mapped_to_model'),
                'rows': [
                    (f'Number of parsed {noun}', it.get('number_of_parsed')),
                    (f'Number of {noun} mapped to model', it.get('number_of_mapped_to_model')),
                    (f'Number of {noun} unmapped to model', it.get('number_of_unmapped_to_model')),
                    (f'Number of unparsed {noun} with error', it.get('number_of_unparsed_with_error')),
                    (f'Number of parsed {noun} with warning', it.get('number_of_parsed_with_warning')),
                ],
                'atom_mapping': rd.atom_name_mapping(it),
            })
        return sfs

    # The most-violated / all-violation lists can hold thousands of rows (both are
    # collapsed panels on screen). Cap them in the static PDF — they are sorted so
    # the most severe come first — and note the truncation.
    cap = _TABLE_ROW_CAP

    def capped(lst):
        return lst[:cap], len(lst)

    most_dist, most_dist_n = capped(rs.get('most_violated_dist_restraints') or [])
    most_dihed, most_dihed_n = capped(rs.get('most_violated_dihed_restraints') or [])
    most_rdc, most_rdc_n = capped(_rdc_most_violated(rs.get('most_violated_rdc_restraints')))
    all_dist, all_dist_n = capped(rs.get('all_dist_violations') or [])
    all_dihed, all_dihed_n = capped(rs.get('all_dihed_violations') or [])
    all_rdc, all_rdc_n = capped(_rdc_most_violated(rs.get('all_rdc_violations')))

    dihed_nonviol_total = _nonviolated(dihed_sum)['total']
    dihed_nonviol_per = [
        {'label': (r.get('restraint_type') or '').capitalize(),
         'count': (r.get('restraint_count') or 0) - (r.get('viol_count') or 0)}
        for r in dihed_sum if (r.get('restraint_type') or '').lower() != 'total'
    ]
    rdc_nonviol_total = _nonviolated(rdc_sum)['total']
    rdc_nonviol_per = [
        {'label': restraint_type_label(r.get('restraint_type') or ''),
         'count': (r.get('restraint_count') or 0) - (r.get('viol_count') or 0)}
        for r in rdc_sum if (r.get('restraint_type') or '').lower() != 'total'
    ]

    return {
        'restraint_available': bool(rs),
        'restraint_props': _restraint_props(rs),
        'dist_per_model_bins': _viol_bins(rs, 'average_number_of_dist_violations_per_model'),
        'dihed_per_model_bins': _viol_bins(rs, 'average_number_of_dihed_violations_per_model'),
        'rdc_per_model_bins': _viol_bins(rs, 'average_number_of_rdc_violations_per_model'),
        'bookkeeping_groups': [
            {'section': sec, 'heading': head, 'empty_text': empty, 'saveframes': bookkeeping(key, noun)}
            for key, sec, head, empty, noun in _BOOKKEEPING_DEFS
        ],
        # 7 / 8 tables
        # Whether any distance / dihedral-angle restraints exist at all (the
        # violation summary lists every restraint category, so an empty summary
        # means there are no restraints of that kind).
        'has_dist': bool(dist_sum),
        'has_dihed': bool(dihed_sum),
        'has_rdc': bool(rdc_sum),
        'dist_summary': dist_sum,
        'dihed_summary': dihed_sum,
        'rdc_summary': rdc_sum,
        'dist_model_cols': _DIST_TABLE_COLS,
        'dist_model_rows': rs.get('dist_violation_for_each_model') or [],
        'dihed_model_cols': _dihed_table_cols(rs.get('dihed_violation_for_each_model')),
        'dihed_model_rows': rs.get('dihed_violation_for_each_model') or [],
        'rdc_model_cols': _rdc_table_cols(rs.get('rdc_violation_for_each_model')),
        'rdc_model_rows': rs.get('rdc_violation_for_each_model') or [],
        'dist_ensemble_cols': _DIST_TABLE_COLS,
        'dist_ensemble_rows': rs.get('dist_violation_for_ensemble') or [],
        'dihed_ensemble_cols': _dihed_table_cols(rs.get('dihed_violation_for_ensemble')),
        'dihed_ensemble_rows': rs.get('dihed_violation_for_ensemble') or [],
        'rdc_ensemble_cols': _rdc_table_cols(rs.get('rdc_violation_for_ensemble')),
        'rdc_ensemble_rows': rs.get('rdc_violation_for_ensemble') or [],
        'dist_nonviol': _nonviolated(dist_sum),
        'dihed_nonviol': {'total': dihed_nonviol_total, 'per_type': dihed_nonviol_per},
        'rdc_nonviol': {'total': rdc_nonviol_total, 'per_type': rdc_nonviol_per},
        'most_violated_dist': most_dist, 'most_violated_dist_total': most_dist_n,
        'most_violated_dihed': most_dihed, 'most_violated_dihed_total': most_dihed_n,
        'most_violated_rdc': most_rdc, 'most_violated_rdc_total': most_rdc_n,
        'all_dist': all_dist, 'all_dist_total': all_dist_n,
        'all_dihed': all_dihed, 'all_dihed_total': all_dihed_n,
        'all_rdc': all_rdc, 'all_rdc_total': all_rdc_n,
        'row_cap': cap,
    }


def _split_notice_css(doc) -> str:
    """Given a first-pass rendered document, return @page:nth(N) rules that place
    'Continued on next page…' / '…continued from previous page' notices on the
    pages where a table spans a page break. Margin-box content does not reflow the
    page, so the first-pass page numbers stay valid for the second pass."""
    pages_of: dict = {}

    def walk(box, pi):
        el = getattr(box, 'element', None)
        if el is not None and getattr(box, 'element_tag', None) == 'table':
            pages_of.setdefault(el, set()).add(pi)
        for c in getattr(box, 'children', None) or []:
            walk(c, pi)
    for pi, page in enumerate(doc.pages):
        walk(page._page_box, pi)

    onward: set = set()   # pages where a table continues onto the next page
    fromprev: set = set()  # pages where a table continued from the previous page
    for pgs in pages_of.values():
        s = sorted(pgs)
        if len(s) < 2:
            continue
        onward.update(s[:-1])
        fromprev.update(s[1:])
    onward.discard(0)  # never annotate the title page
    fromprev.discard(0)
    if not onward and not fromprev:
        return ''

    style = 'font-size:7.5pt;font-style:italic;color:#64748b'
    rules = []
    for pi in sorted(onward | fromprev):
        boxes = []
        if pi in fromprev:
            boxes.append(f'@bottom-left {{ content: "\\2026 continued from previous page"; {style} }}')
        if pi in onward:
            boxes.append(f'@bottom-right {{ content: "Continued on next page \\2026"; {style} }}')
        rules.append(f'@page:nth({pi + 1}) {{ {" ".join(boxes)} }}')
    return '\n'.join(rules)


def build_chem_shift_sections(stats: dict, sf_charts: dict) -> list:
    """Full Section 5 content, one entry per chemical-shift saveframe: bookkeeping
    counts, atom-name-mapping history, completeness pivots, the statistically
    unusual (outlier) shifts, and the unmapped/unparsed/duplicated shift tables,
    plus the saveframe's chart ids. Mirrors the download page's chemShiftSaveframes."""
    import report_data as rd

    def has_ins(rows):
        return any((r or {}).get('ins_code') not in (None, '') for r in rows)

    sections = []
    for st in stats.get('chem_shift', []) or []:
        lid = st.get('list_id')
        outlier = st.get('chemical_shift_outlier') or []
        unmapped = st.get('chemical_shift_unmapped') or []
        unparsed = st.get('chemical_shift_unparsed') or []
        duplicated = st.get('chemical_shift_duplicated') or []
        completeness = []
        for phrase, region in (
            ('well-defined regions of the structure', st.get('completeness_in_well_defined_region')),
            ('full structure', st.get('completeness_in_full_length_region')),
        ):
            view = rd.completeness_view(region)
            if view:
                completeness.append({'phrase': phrase, 'view': view})
        sections.append({
            'list_id': lid,
            'sf_framecode': st.get('sf_framecode'),
            'original_file_name': st.get('original_file_name'),
            'bookkeeping': [
                ('Number of parsed shifts', st.get('number_of_parsed')),
                ('Number of shifts mapped to model', st.get('number_of_mapped_to_model')),
                ('Number of shifts unmapped to model', st.get('number_of_unmapped_to_model')),
                ('Number of unparsed shifts with error', st.get('number_of_unparsed_with_error')),
                ('Number of parsed shifts with warning', st.get('number_of_parsed_with_warning')),
                ('Number of chemical shift outliers', st.get('number_of_outliers')),
            ],
            'atom_mapping': rd.atom_name_mapping(st),
            'completeness': completeness,
            'outliers': outlier,
            'outlier_count': st.get('number_of_outliers') if st.get('number_of_outliers') is not None else len(outlier),
            'show_outlier_ins': has_ins(outlier),
            'show_outlier_details': any((o or {}).get('details') not in (None, '') for o in outlier),
            'unmapped': unmapped,
            'unmapped_count': (
                st.get('number_of_unmapped_to_model')
                if st.get('number_of_unmapped_to_model') is not None else len(unmapped)
            ),
            'show_unmapped_ins': has_ins(unmapped),
            'unparsed': unparsed,
            'unparsed_count': (
                st.get('number_of_unparsed_with_error')
                if st.get('number_of_unparsed_with_error') is not None else len(unparsed)
            ),
            'show_unparsed_ins': has_ins(unparsed),
            'duplicated': duplicated,
            'duplicated_count': len(duplicated),
            'show_duplicated_ins': has_ins(duplicated),
            'charts': sf_charts.get(lid, []),
        })
    return sections


def render_charts(specs: list, work_dir: Path) -> dict:
    """Run the Node SSR renderer; return {id: svg_markup} for charts that
    produced an SVG (null entries — no data — are dropped)."""
    work_dir.mkdir(parents=True, exist_ok=True)
    inputs_path = work_dir / 'chart_inputs.json'
    inputs_path.write_text(json.dumps(specs), encoding='utf-8')
    svg_dir = work_dir / 'charts'

    proc = subprocess.run(
        ['node', str(RENDERER), str(inputs_path), str(svg_dir)],
        capture_output=True, text=True, timeout=600,
    )
    if proc.stderr:
        print(proc.stderr, file=sys.stderr)
    if proc.returncode != 0:
        raise RuntimeError(f'chart renderer failed (exit {proc.returncode})')

    manifest = json.loads((svg_dir / 'charts.json').read_text(encoding='utf-8'))
    svgs = {}
    for cid, fname in manifest.items():
        if fname:
            svgs[cid] = (svg_dir / fname).read_text(encoding='utf-8')
    return svgs


# --------------------------------------------------------------- context --- #

def build_context(stats: dict, ensemble: dict, provenance: dict,
                  charts: dict, timestamp: str) -> dict:
    rs = stats.get('restraint_summary', {}) or {}
    clusters = ensemble.get('cluster_analysis') or []
    regions = ensemble.get('well_defined_region') or []
    single_cluster = next((c for c in clusters if c.get('cluster_id') == -1), None)
    ensemble_caption = {
        'total': ensemble.get('total_models'),
        'medoid': regions[0].get('medoid_model_id') if regions else None,
        'representative': ensemble.get('representative_model_id'),
        'criteria': ensemble.get('selection_criteria'),
        'clustered_count': sum(1 for c in clusters if c.get('cluster_id') != -1),
        'single_model_count': len(single_cluster.get('model_ids') or []) if single_cluster else 0,
    }
    # Chemical-shift summary (section 4): well-defined + full-length groups
    # (mirrors chemShiftProps / chemShiftFullProps: assignedOf / outliers / pct).
    cs = stats.get('chem_shift_summary') or {}

    def _assigned_of(a, t):
        return None if a is None else (str(a) if t is None else f'{a} of {t}')

    def _outliers(a, f):
        return None if (a is None or f is None) else a - f

    def _pct(v):
        return None if v is None else f'{v * 100:.1f}%'

    def _cs_props(suffix, label_suffix):
        a = cs.get(f'number_of_assigned_shifts_in_{suffix}')
        return [
            (f'Total number of shifts ({label_suffix})',
             _assigned_of(a, cs.get(f'number_of_target_shifts_in_{suffix}'))),
            (f'Number of shift outliers ({label_suffix})',
             _outliers(a, cs.get(f'number_of_favorable_assigned_shifts_in_{suffix}'))),
            (f'Completeness of assignment ({label_suffix})',
             _pct(cs.get(f'completeness_in_{suffix}'))),
        ]

    conv_id = provenance.get('conversion_id')
    public_id = f'C_{conv_id}' if conv_id is not None else ''
    depsys = provenance.get('target_depsys')

    return {
        'public_id': public_id,
        'output_used_for': TARGET_DEPSYS_LABEL.get(depsys, depsys or ''),
        'processed_site': stats.get('processed_site') or provenance.get('processed_site', ''),
        'timestamp': timestamp,
        'input_files': provenance.get('input_files', []) or [],
        'show_input_source': any(
            (f or {}).get('source', 'user') != 'user'
            for f in provenance.get('input_files', []) or []
        ),
        'stats': stats,
        'ensemble': ensemble,
        'ensemble_caption': ensemble_caption,
        'chem_shift_props': _cs_props('well_defined_region', 'well-defined region'),
        'chem_shift_full_props': _cs_props('full_length_region', 'full-length'),
        'software': stats.get('software', []) or [],
        'model': stats.get('model'),
        'restraint_summary': rs,
        # Straightforward violation summary tables (rendered generically).
        'dist_violation_summary': rs.get('dist_violation_summary', []) or [],
        'dihed_violation_summary': rs.get('dihed_violation_summary', []) or [],
        'charts': charts,
    }


# ------------------------------------------------------------------ main --- #

def main() -> int:
    ap = argparse.ArgumentParser(description='Render the conversion report PDF.')
    ap.add_argument('--report', required=True, help='NMR data-processing JSON report')
    ap.add_argument('--out', required=True, help='output PDF path')
    ap.add_argument('--provenance', help='provenance JSON (input files, depsys, …)')
    ap.add_argument('--work-dir', help='scratch dir for chart_inputs/SVGs (default: alongside --out)')
    args = ap.parse_args()

    report_path = Path(args.report)
    out_path = Path(args.out)
    work_dir = Path(args.work_dir) if args.work_dir else out_path.parent / '_pdf_work'

    report = json.loads(report_path.read_text(encoding='utf-8'))
    provenance = {}
    if args.provenance and Path(args.provenance).is_file():
        provenance = json.loads(Path(args.provenance).read_text(encoding='utf-8'))

    from jinja2 import Environment, FileSystemLoader, select_autoescape
    from markupsafe import Markup
    from weasyprint import CSS, HTML

    stats = output_statistics(report)
    ensemble = ensemble_composition(report)

    specs = build_chart_inputs(stats, ensemble)
    cs_specs, cs_by_sf = build_chem_shift_charts(stats)
    specs += cs_specs
    corr_specs, rdc_correlations = build_rdc_correlation(report)
    specs += corr_specs
    charts = render_charts(specs, work_dir)

    ctx = build_context(stats, ensemble, provenance, charts,
                        report_timestamp_utc(report_path))
    ctx['chem_shift_sections'] = build_chem_shift_sections(stats, cs_by_sf)
    ctx['r'] = build_restraint_sections(stats)
    ctx['rdc_correlations'] = rdc_correlations
    icon_path = ASSETS_DIR / 'report_logo.png'
    if icon_path.is_file():
        import base64
        b64 = base64.b64encode(icon_path.read_bytes()).decode('ascii')
        ctx['service_icon'] = f'data:image/png;base64,{b64}'
    else:
        ctx['service_icon'] = ''

    env = Environment(
        loader=FileSystemLoader(str(TEMPLATES_DIR)),
        autoescape=select_autoescape(['html', 'xml']),
    )
    # Chart / icon SVGs are trusted markup we generated; mark safe so autoescape
    # inlines them as elements instead of escaping the angle brackets to text.
    env.filters['safe_svg'] = lambda s: Markup(s) if s else ''
    env.filters['restraint_type_label'] = restraint_type_label
    env.filters['pct1'] = lambda v: '' if v is None else f'{v:.1f}'
    env.filters['yesno'] = lambda v: None if v is None else ('Yes' if v else 'No')
    env.filters['formatsize'] = _format_size
    import report_data as _rd
    env.filters['input_type_label'] = _rd.input_file_type_label
    env.filters['output_type_label'] = _rd.output_file_type_label
    html = env.get_template('report.html').render(**ctx)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    base_css = CSS(filename=str(TEMPLATES_DIR / 'report.css'))
    # Pass 1: lay out to find which tables span page breaks; Pass 2: re-render
    # with per-page "Continued…" notices (margin content, so layout is unchanged).
    doc = HTML(string=html, base_url=str(TEMPLATES_DIR)).render(stylesheets=[base_css])
    extra = _split_notice_css(doc)
    if extra:
        HTML(string=html, base_url=str(TEMPLATES_DIR)).write_pdf(
            str(out_path), stylesheets=[base_css, CSS(string=extra)],
        )
    else:
        doc.write_pdf(str(out_path))
    print(f'[generate_report] wrote {out_path}')
    return 0


if __name__ == '__main__':
    sys.exit(main())

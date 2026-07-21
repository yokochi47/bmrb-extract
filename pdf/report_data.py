"""Chemical-shift chart data-shaping for the PDF report (Section 5).

These functions turn the raw report JSON's `random_coil_index` / `histogram`
into the PerResidueLine / HistogramChart shapes the shared chart builders
(report-charts.ts, via render_charts.mjs) consume — the RCI/S² and NMR-RMSD
per-residue line charts and the Z-score chemical-shift histograms.

PORTED VERBATIM from backend/app/app.py (the /api/output_statistics endpoint's
`_rci_charts` / `_histogram_chart` and their dependencies). Keep in sync with
app.py if that shaping changes — both must agree so the PDF matches the web
report. Only the chem-shift subset is ported here (no discrepancy / dihedral /
per-residue-constraint charts, which the PDF does not render).
"""
import re

_SUPERSCRIPT = str.maketrans('0123456789', '⁰¹²³⁴⁵⁶⁷⁸⁹')
_ISOTOPE_RE = re.compile(r'(\d+)([a-zA-Z]+)')
_STRUCT_CONF_TYPES = {'HELX': 'helix', 'STRN': 'strand', 'TURN': 'turn'}


def _normalize_label(key):
    """Format an isotope-bearing key (e.g. '1h_chemical_shifts') into a
    superscript-mass element label (¹H / ¹³C / ¹⁵N); otherwise prettify."""
    m = _ISOTOPE_RE.search(key)
    if m:
        return m.group(1).translate(_SUPERSCRIPT) + m.group(2).upper()
    key = key[0].upper() + key[1:]
    key = (key.replace('_constraints', '')
           .replace('backbone-backbone', '(bb-bb)')
           .replace('backbone-sidechain', '(bb-sc)')
           .replace('sidechain-sidechain', '(sc-sc)'))
    return key.replace('_', ' ').strip()


def _constraint_order(key):
    """Canonical display order for a distance-restraint constraint-type key;
    non-distance keys (chem-shift isotopes) fall to the end (stable)."""
    k = key.lower()
    if 'backbone-backbone' in k:
        sub = 0
    elif 'backbone-sidechain' in k:
        sub = 1
    elif 'sidechain-sidechain' in k:
        sub = 2
    else:
        sub = 3
    if k.startswith('intra'):
        return (0, 0)
    if k.startswith('sequential'):
        return (1, sub)
    if k.startswith('medium'):
        return (2, sub)
    if k.startswith('long'):
        return (3, 0)
    if k.startswith('inter'):
        return (4, 0)
    if k.startswith('symmetric'):
        return (5, 0)
    return (99, 0)


def _annotation_x(value, rov, inverse=False):
    """Precise fractional position of `value` on the hidden marker axis (0 … n+1)
    overlaying the histogram's category axis."""
    r0, rn, n = rov[0], rov[-1], len(rov)
    if rn == r0:
        return 0.0
    frac = (value - r0) / (rn - r0) * n
    return max(0.0, min(n + 1.0, n + 1.0 - frac if inverse else frac))


def _histogram_annotations(h, inverse=False):
    """Per-outlier annotations (dashed marker + label) for a Z-score histogram."""
    rov = h.get('range_of_values') or []
    ann = h.get('annotations') or []
    if len(rov) < 2 or not ann:
        return []
    scale = rov[1] - rov[0]
    out = []
    for a in sorted(ann, key=lambda x: -(x.get('z_score') or 0)):
        z = a.get('z_score')
        if not isinstance(z, (int, float)) or not scale:
            continue
        out.append({
            'x': _annotation_x(z, rov, inverse),
            'anomalous': a.get('level') == 'anomalous',
            'text': (f"{a.get('chain_id')}:{a.get('seq_id')}:{a.get('comp_id')}:"
                     f"{a.get('atom_id')}, {a.get('value')} ppm, Z score {z}"),
        })
    return out


def histogram_chart(stat_list, inverse=False, annotate=_histogram_annotations):
    """Build [{label, categories, series, annotations}] from a stats list's
    `histogram` ({range_of_values, number_of_values: {key: [counts]}})."""
    charts = []
    for st in stat_list or []:
        h = st.get('histogram')
        if not isinstance(h, dict) or not h.get('range_of_values'):
            continue
        rov = h.get('range_of_values')
        scale = rov[1] - rov[0]
        categories = [f'({v + scale}, {v}]' if inverse else f'[{v}, {v + scale})'
                      for v in h['range_of_values']]
        nov = h.get('number_of_values') or {}
        series = [
            {'name': _normalize_label(k), 'data': v}
            for k, v in sorted(nov.items(), key=lambda kv: _constraint_order(kv[0]))
            if isinstance(v, list) and any(v)
        ]
        if series:
            charts.append({'label': st.get('sf_framecode', ''),
                           'categories': categories, 'series': series,
                           'annotations': annotate(h, inverse)})
    return charts


def _sc_type(sc):
    """Secondary-structure class from a struct_conf token (e.g. 'HELX_P:AA1')."""
    if not sc:
        return None
    head = re.split(r'[_:]', str(sc), 1)[0].upper()
    return _STRUCT_CONF_TYPES.get(head)


def _struct_conf_bands(struct_conf):
    """Collapse runs of the same struct_conf value into colored bands
    [{start, end, type, label}] (indices into the residue list)."""
    bands = []
    sc_list = struct_conf or []
    i, n = 0, len(sc_list)
    while i < n:
        typ = _sc_type(sc_list[i])
        if typ is None:
            i += 1
            continue
        j = i
        while j + 1 < n and sc_list[j + 1] == sc_list[i]:
            j += 1
        label = sc_list[i]
        bands.append({'start': i, 'end': j, 'type': typ, 'label': label})
        i = j + 1
    return bands


def _domain_bands(domain_id):
    """Collapse runs of the same domain_id into bands [{start, end, type, label}].
    domain_id > 0 → 'core'; == -1 → 'unmodeled'; else → gap (no band)."""
    bands = []
    dom = domain_id or []
    i, n = 0, len(dom)
    while i < n:
        d = dom[i]
        if not isinstance(d, int) or (d != -1 and d <= 0):
            i += 1
            continue
        j = i
        while j + 1 < n and dom[j + 1] == d:
            j += 1
        if d == -1:
            bands.append({'start': i, 'end': j, 'type': 'unmodeled',
                          'label': 'unmodeled residues'})
        else:
            bands.append({'start': i, 'end': j, 'type': 'core',
                          'label': f'well-defined core {d}'})
        i = j + 1
    return bands


# --- Completeness of resonance assignments (5.x.2) --------------------------- #
# Mirrors the frontend COMPLETENESS_ROWS / NUCLEUS_COLUMNS / buildCompletenessView.

COMPLETENESS_ROWS = [
    ('completeness_of_backbone_assignments', 'Backbone'),
    ('completeness_of_sidechain_assignments', 'Sidechain'),
    ('completeness_of_aromatic_assignments', 'Aromatic'),
    ('completeness_of_sugar_assignments', 'Sugar'),
    ('completeness_of_base_assignments', 'Base'),
    ('completeness_of_overall_assignments', 'Overall'),
]
NUCLEUS_COLUMNS = ['Total', '¹H', '¹³C', '¹⁵N', '³¹P']


def _nucleus_column(atom_group):
    g = (atom_group or '').lower()
    if '1h' in g:
        return '¹H'
    if '13c' in g:
        return '¹³C'
    if '15n' in g:
        return '¹⁵N'
    if '31p' in g:
        return '³¹P'
    return 'Total'


def _fmt_completeness_cell(e):
    if not e:
        return '– / –'
    a = e.get('number_of_assigned_shifts') or 0
    t = e.get('number_of_target_shifts') or 0
    comp = e.get('completeness')
    if comp is not None:
        pct = round(comp * 100)
    elif t:
        pct = round(a / t * 100)
    else:
        pct = None
    return f'{a}/{t}' if pct is None else f'{a}/{t} ({pct}%)'


def _by_nucleus(entries):
    m = {}
    for e in entries or []:
        col = _nucleus_column(e.get('atom_group'))
        if col not in m:
            m[col] = e
    return m


def completeness_view(region):
    """Pivot a completeness-region object into {columns, rows, overall_*, stereo}
    or None when empty (mirrors buildCompletenessView)."""
    if not region:
        return None
    present = set()
    for key, _ in COMPLETENESS_ROWS:
        for e in region.get(key) or []:
            if e.get('atom_group'):
                present.add(_nucleus_column(e['atom_group']))
    if not present:
        return None
    columns = [n for n in NUCLEUS_COLUMNS if n in present]
    rows = []
    for key, label in COMPLETENESS_ROWS:
        arr = region.get(key)
        if not arr:
            continue
        bg = _by_nucleus(arr)
        rows.append({'label': label, 'cells': [_fmt_completeness_cell(bg.get(c)) for c in columns]})
    if not rows:
        return None
    overall = _by_nucleus(region.get('completeness_of_overall_assignments')).get('Total')
    stereo_by = _by_nucleus(region.get('completeness_of_stereomethyl_assignments'))
    stereo = stereo_by.get('Total') or (region.get('completeness_of_stereomethyl_assignments') or [None])[0]
    return {
        'columns': columns,
        'rows': rows,
        'overall_pct': (round(overall['completeness'] * 100)
                        if overall and overall.get('completeness') is not None else None),
        'overall_assigned': overall.get('number_of_assigned_shifts') if overall else None,
        'overall_target': overall.get('number_of_target_shifts') if overall else None,
        'stereo': ({'assigned': stereo.get('number_of_assigned_shifts') or 0,
                    'target': stereo.get('number_of_target_shifts') or 0} if stereo else None),
    }


# --- Atom-name mapping history (5.x.1) --------------------------------------- #
# Ported from the backend _atom_name_mapping / _atom_mapping_normal.

def _atom_mapping_normal(atom_name, atom_ids):
    """Whether an original → IUPAC atom-name mapping looks expected."""
    if not atom_name or not atom_ids:
        return True

    def matches(name):
        return bool(name) and (
            any(a.startswith(name) for a in atom_ids)
            or any(name.startswith(a) for a in atom_ids)
        )

    if matches(atom_name):
        return True
    name = atom_name.replace('#', '').replace('%', '').replace('*', '')
    if name and name[0] in ('Q', 'M'):
        name = 'H' + name[1:]
    if name and name[0] in ('1', '2', '3'):
        name = name[1:] + name[0]
    if matches(name):
        return True
    if (len(name) > 2 and name[-1].isdigit()
            and (not name[-2].isdigit() or atom_ids[0].startswith(name[:-1]))):
        if matches(name[:-1]):
            return True
    return False


def atom_name_mapping(st):
    """[{comp_id, history:[{name, atoms, unusual}]}] for one saveframe."""
    out = []
    for m in st.get('atom_name_mapping') or []:
        history = []
        for h in m.get('history') or []:
            name = h.get('atom_name', '')
            atom_ids = [str(a) for a in (h.get('atom_id') or [])]
            history.append({'name': name, 'atoms': ', '.join(atom_ids),
                            'unusual': not _atom_mapping_normal(name, atom_ids)})
        if history:
            out.append({'comp_id': m.get('comp_id', ''), 'history': history})
    return out


def rci_charts(chem_shift_list, auth=False):
    """RCI/S² (0–1) and NMR-RMSD (Å, with well-defined-region threshold)
    per-residue line charts from `random_coil_index`. auth=True keys residues by
    auth_chain_id/auth_seq_id (the coordinate scheme / download page)."""
    chain_key = 'auth_chain_id' if auth else 'chain_id'
    seq_key = 'auth_seq_id' if auth else 'seq_id'
    charts = []
    for st in chem_shift_list or []:
        for rci in st.get('random_coil_index') or []:
            seq = rci.get(seq_key) or []
            comp = rci.get('comp_id') or []
            if not seq:
                continue
            cats = [f"{comp[i] if i < len(comp) else ''} {seq[i]}".strip() for i in range(len(seq))]
            bands = _struct_conf_bands(rci.get('struct_conf'))
            chain = rci.get(chain_key)
            order = [
                {'name': nm, 'data': rci[k]}
                for k, nm in (('rci', 'RCI'), ('s2', 'S²'))
                if isinstance(rci.get(k), list) and any(x is not None for x in rci[k])
            ]
            sf = st.get('sf_framecode', '')
            if order:
                charts.append({'chain': chain, 'label': 'RCI / S²', 'sf': sf, 'categories': cats,
                               'series': order, 'bands': bands, 'ymin': 0, 'ymax': 1, 'threshold': None})
            rmsd = rci.get('nmr_rmsd')
            if isinstance(rmsd, list) and any(x is not None for x in rmsd):
                thr = rci.get('rmsd_in_well_defined_region')
                rmsd_vals = [x for x in rmsd if isinstance(x, (int, float))]
                ymax = max(max(rmsd_vals), 3.0) if rmsd_vals else 3.0
                charts.append({'chain': chain, 'label': 'NMR RMSD (Å)', 'sf': sf, 'categories': cats,
                               'series': [{'name': 'NMR RMSD', 'data': rmsd}],
                               'bands': _domain_bands(rci.get('domain_id')),
                               'ymin': 0, 'ymax': ymax,
                               'threshold': round(thr, 2) if isinstance(thr, (int, float)) else None})
    return charts

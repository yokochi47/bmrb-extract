/**
 * Upload file-type catalogue: the canonical mapping of upload_file_type enum
 * values to their human-readable labels. Single source of truth shared by the
 * upload page (file-type select) and the summary page (file table).
 */
export interface FileTypeOption {
  label: string;
  value: string;
}

export const FILE_TYPE_OPTIONS: readonly FileTypeOption[] = [
  // Coordinate
  { label: 'Coordinates (PDBx/mmCIF format)', value: 'co-cif' },
  { label: 'Coordinates (PDB format)', value: 'co-pdb' },
  // Assigned chemical shifts
  { label: 'Assigned chemical shifts (NMR-STAR V3 format)', value: 'nm-shi' },
  { label: 'Assigned chemical shifts (ARIA format)', value: 'nm-shi-ari' },
  { label: 'Assigned chemical shifts (GARRET format)', value: 'nm-shi-gar' },
  { label: 'Assigned chemical shifts (NMRPIPE format)', value: 'nm-shi-npi' },
  { label: 'Assigned chemical shifts (OLIVIA format)', value: 'nm-shi-oli' },
  { label: 'Assigned chemical shifts (PIPP format)', value: 'nm-shi-pip' },
  { label: 'Assigned chemical shifts (NMRVIEW/CAMRA format)', value: 'nm-shi-ppm' },
  { label: 'Assigned chemical shifts (NMR-STAR V2 format, seq+cs loop)', value: 'nm-shi-st2' },
  { label: 'Assigned chemical shifts (XEASY format, aka. prot)', value: 'nm-shi-xea' },
  {
    label:
      "Assigned chemical shifts (WSV/TSV/CSV; Residue per line, Atom per line, or SPARKY's list)",
    value: 'nm-shi-bar',
  },
  // NMR restraints
  { label: 'NMR restraints (AMBER format)', value: 'nm-res-amb' },
  { label: 'NMR restraints (ARIA format)', value: 'nm-res-ari' },
  { label: 'NMR restraints (ARIA XML format)', value: 'nm-res-arx' },
  {
    label: "NMR restraints (WSV/TSV/CSV with a header; MARDIGAS, AQUA's noe, or User-defined)",
    value: 'nm-res-bar',
  },
  { label: 'NMR restraints (BIOSYM format, incl. INSIGHT-II)', value: 'nm-res-bio' },
  { label: 'NMR restraints (CHARMM format)', value: 'nm-res-cha' },
  { label: 'NMR restraints (CNS format)', value: 'nm-res-cns' },
  { label: 'NMR restraints (CYANA format)', value: 'nm-res-cya' },
  { label: 'NMR restraints (CYANA NOA format, aka. noe assignment)', value: 'nm-res-noa' },
  { label: 'NMR restraints (DYNAMO/PALES/TALOS format)', value: 'nm-res-dyn' },
  { label: 'NMR restraints (GROMACS format)', value: 'nm-res-gro' },
  { label: 'NMR restraints (ISD format)', value: 'nm-res-isd' },
  { label: 'NMR restraints (ROSETTA format)', value: 'nm-res-ros' },
  {
    label: 'NMR restraints (SAXS profile containing columns for q, I(q), σ(I))',
    value: 'nm-res-sax',
  },
  { label: 'NMR restraints (Schröginder/ASL format)', value: 'nm-res-sch' },
  { label: 'NMR restraints (SYBYL format)', value: 'nm-res-syb' },
  { label: 'NMR restraints (XPLOR-NIH format)', value: 'nm-res-xpl' },
  { label: 'NMR restraints (other plane text format)', value: 'nm-res-oth' },
  // Topology
  { label: 'Topology (AMBER format)', value: 'nm-aux-amb' },
  { label: 'Topology (CHARMM format)', value: 'nm-aux-cha' },
  { label: 'Topology (GROMACS format)', value: 'nm-aux-gro' },
  { label: 'Topology (PDB format)', value: 'nm-aux-pdb' },
  { label: 'Topology (XEASY format, aka. prot)', value: 'nm-aux-xea' },
  // Spectral peak lists
  { label: 'Spectral peak list (ARIA format)', value: 'nm-pea-ari' },
  { label: 'Spectral peak list (CCPN format)', value: 'nm-pea-ccp' },
  { label: 'Spectral peak list (OLIVIA format)', value: 'nm-pea-oli' },
  { label: 'Spectral peak list (NMRPIPE/PIPP format)', value: 'nm-pea-pip' },
  { label: 'Spectral peak list (PONDEROSA format)', value: 'nm-pea-pon' },
  { label: 'Spectral peak list (SPARKY format)', value: 'nm-pea-spa' },
  { label: "Spectral peak list (SPARKY's save format, aka. ornament)", value: 'nm-pea-sps' },
  { label: 'Spectral peak list (TOPSPIN format)', value: 'nm-pea-top' },
  { label: 'Spectral peak list (NMRVIEW format)', value: 'nm-pea-vie' },
  { label: 'Spectral peak list (VNMR format)', value: 'nm-pea-vnm' },
  { label: 'Spectral peak list (XEASY format)', value: 'nm-pea-xea' },
  { label: 'Spectral peak list (XWINNMR format)', value: 'nm-pea-xwi' },
  { label: 'Spectral peak list (WSV/TSV with a header)', value: 'nm-pea-bar' },
  {
    label: 'Spectral peak list (any plane text format, auto format detection)',
    value: 'nm-pea-any',
  },
  // NMR unified data
  { label: 'NMR unified data (NEF: NMR Exchange Format)', value: 'nm-uni-nef' },
  { label: 'NMR unified data (NMR-STAR V3 format)', value: 'nm-uni-str' },
];

const FILE_TYPE_LABELS = new Map(FILE_TYPE_OPTIONS.map((o) => [o.value, o.label]));

/** Human-readable label for an upload file-type value; falls back to the raw
 * value if it is unknown. */
export function fileTypeLabel(value: string | null): string {
  if (!value) return '';
  return FILE_TYPE_LABELS.get(value) ?? value;
}

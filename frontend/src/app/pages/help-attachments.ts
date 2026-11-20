/** Help-desk attachment rules. Mirrors backend/app/core/helpdesk.py — the
 * server enforces them; these only give early feedback in the form. */

export const HELP_ATTACH_MAX_FILES = 3;
export const HELP_ATTACH_MAX_BYTES = 10 * 1024 * 1024;
export const HELP_ATTACH_SUFFIXES = [
  '.txt',
  '.log',
  '.csv',
  '.json',
  '.pdf',
  '.png',
  '.jpg',
  '.jpeg',
  '.str',
  '.nef',
  '.star',
  '.cif',
  '.mmcif',
  '.pdb',
  '.mr',
  '.tbl',
  '.zip',
  '.gz',
  '.tgz',
  '.tar',
];
/** For the <input type="file" accept> attribute. */
export const HELP_ATTACH_ACCEPT = HELP_ATTACH_SUFFIXES.join(',');

export interface HelpAttachment {
  ordinal: number;
  name: string;
  size: number;
  expires_at: string | null;
  expired: boolean;
}

export function formatBytes(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / 1024 / 1024).toFixed(1)} MB`;
}

/** Merge newly picked files into the current selection; returns the new list
 * and an error message for anything rejected (null when all were accepted). */
export function addAttachments(
  current: File[],
  picked: FileList | null,
): { files: File[]; error: string | null } {
  const files = [...current];
  const errors: string[] = [];
  for (const f of Array.from(picked ?? [])) {
    const lower = f.name.toLowerCase();
    if (!HELP_ATTACH_SUFFIXES.some((s) => lower.endsWith(s))) {
      errors.push(`${f.name}: file type not allowed`);
    } else if (f.size > HELP_ATTACH_MAX_BYTES) {
      errors.push(`${f.name}: larger than ${formatBytes(HELP_ATTACH_MAX_BYTES)}`);
    } else if (files.length >= HELP_ATTACH_MAX_FILES) {
      errors.push(`${f.name}: at most ${HELP_ATTACH_MAX_FILES} files per message`);
    } else if (!files.some((x) => x.name === f.name && x.size === f.size)) {
      files.push(f);
    }
  }
  return { files, error: errors.length ? errors.join('; ') : null };
}

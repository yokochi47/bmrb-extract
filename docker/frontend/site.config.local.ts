/**
 * Site configuration for the local deployment — resolved at RUNTIME.
 *
 * Upstream generates `src/site.config.ts` from a per-site template before the
 * Angular build, which bakes the values into the bundle: changing one means
 * rebuilding. This variant keeps the same exported surface but reads it from a
 * single placeholder that the nginx container substitutes on every start, so
 * `docker compose up -d` after editing `.env` is enough.
 *
 * The placeholder is base64 so the substituted payload can never contain a
 * quote, backslash or newline that would break out of the string literal it
 * lands in — whichever quote style the minifier happened to choose.
 *
 * Every value falls back to a sane default, so an unsubstituted bundle (running
 * `ng serve` straight from a checkout, for instance) still boots.
 *
 * See docker/nginx/entrypoint.d/20-site-config.sh for the substitution.
 */

const ENCODED_CONFIG = '@@BMRBX_SITE_CONFIG_B64@@';

function decodeConfig(): Record<string, string> {
  try {
    const binary = atob(ENCODED_CONFIG);
    // Re-decode as UTF-8 so non-ASCII branding survives the round trip.
    const json = decodeURIComponent(
      Array.from(binary, (ch) => '%' + ch.charCodeAt(0).toString(16).padStart(2, '0')).join(''),
    );
    return JSON.parse(json) as Record<string, string>;
  } catch {
    return {};
  }
}

const CONFIG = decodeConfig();

function text(key: string, fallback: string): string {
  const value = CONFIG[key];
  return value === undefined || value === '' ? fallback : value;
}

function number(key: string, fallback: number): number {
  const value = Number(CONFIG[key]);
  return Number.isFinite(value) ? value : fallback;
}

export const HOST_SITE_NAME = text('HOST_SITE_NAME', 'BMRB');
export const HOST_SITE_LOGO = text('HOST_SITE_LOGO', 'bmrb_logo.png');
export const HOST_SITE_URL = text('HOST_SITE_URL', 'https://bmrb.io');
export const ONEDEP_URL = text('ONEDEP_URL', 'https://deposit.wwpdb.org');
export const BMRBDEP_URL = text('BMRBDEP_URL', 'https://deposit.bmrb.io');
export const SERVICE_HELP_EMAIL = text('SERVICE_HELP_EMAIL', 'help@bmrb.io');
export const SUCCESS_VALIDITY_PERIOD_IN_DAYS = number('SUCCESS_VALIDITY_PERIOD_IN_DAYS', 420);
export const FAILURE_VALIDITY_PERIOD_IN_DAYS = number('FAILURE_VALIDITY_PERIOD_IN_DAYS', 60);
export const FRONTEND_VERSION = text('FRONTEND_VERSION', '0.0.0');
export const SERVICE_LEVEL: 'production' | 'development' =
  text('SERVICE_LEVEL', 'development') === 'production' ? 'production' : 'development';

/** Browser-facing Flask API base path, proxied by nginx. Never the internal URL. */
export const API_URL = '/api/';

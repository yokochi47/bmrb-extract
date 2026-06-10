# Site specific configurations

SERVICE_LEVEL = 'development'

assert SERVICE_LEVEL in ('production', 'development')

SERVICE_DOMAIN = 'pdbj.org'
SERVICE_HOST = 'bmrb-extract.pdbj.org'

SERVICE_ADMIN_EMAIL= 'bmrbsys@protein.osaka-u.ac.jp'
SERVICE_HELP_EMAIL = 'bmrbhelp@protein.osaka-u.ac.jp'

SECRET_KEY = 'da520e67-025e-4756-981c-127de33469a4'
SMTP_SERVER = 'postman.protein.osaka-u.ac.jp'
CONV_ID_RANGE_BEGIN = 8000001
CONV_ID_RANGE_END = 9000000

assert 1000000 <= CONV_ID_RANGE_BEGIN < CONV_ID_RANGE_END <= 9000000
assert CONV_ID_RANGE_END - CONV_ID_RANGE_BEGIN == 999999

SERVICE_DATABASE_URL = 'postgresql+asyncpg://webmaster:bmrbnmr@postgres:5432/internal'
FLASK_API_URL = 'http://backend:8000/api/'

SUCCESS_VALIDITY_PERIOD_IN_DAYS = 420
FAILURE_VALIDITY_PERIOD_IN_DAYS = 60

ARCHIVE_BASE_PATH = '/archive'


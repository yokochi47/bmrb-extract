# This code is generated from postgres/init-service.sql.template
# by omymodels Online (https://archon-omymodels-online.hf.space)
import sqlalchemy as sa
from sqlalchemy.ext.declarative import declarative_base
from enum import Enum
from sqlalchemy.sql import func
from sqlalchemy.dialects.postgresql import UUID, INET, ENUM

from core.site_config import SERVICE_DOMAIN


Base = declarative_base()


class EnumStr(sa.types.TypeDecorator):
    """Map a native PostgreSQL ENUM column as a plain Python str.

    Hand-added (not produced by omymodels): the generator emits sa.Text() for
    these enum columns, but asyncpg refuses to send a text value into a native
    enum column. impl=Text keeps reads as str (manifest JSON and the
    file_type.startswith checks rely on this); bind_expression casts bound
    values to the enum type in SQL so writes succeed. Re-apply after
    regenerating this file.
    """

    impl = sa.Text
    cache_ok = True

    def __init__(self, pg_type_name, **kwargs):
        self._pg_type_name = pg_type_name
        super().__init__(**kwargs)

    def bind_expression(self, bindvalue):
        return sa.cast(bindvalue, ENUM(name=self._pg_type_name, create_type=False))


class ProcessingSiteCode(str, Enum):

    bmrb_io = 'bmrb.io'
    pdbj_org = 'pdbj.org'


class SessionStatusCode(str, Enum):

    completed = 'completed'
    created = 'created'
    expired = 'expired'
    failed = 'failed'
    processing = 'processing'
    uploading = 'uploading'


class TargetDepsysCode(str, Enum):

    bmrbdep = 'bmrbdep'
    onedep = 'onedep'
    repl_cs = 'repl_cs'


class UploadFileType(str, Enum):

    co_cif = 'co-cif'
    co_pdb = 'co-pdb'
    nm_aux_amb = 'nm-aux-amb'
    nm_aux_cha = 'nm-aux-cha'
    nm_aux_gro = 'nm-aux-gro'
    nm_aux_pdb = 'nm-aux-pdb'
    nm_aux_xea = 'nm-aux-xea'
    nm_csp_ari = 'nm-csp-ari'
    nm_csp_bar = 'nm-csp-bar'
    nm_csp_gar = 'nm-csp-gar'
    nm_csp_npi = 'nm-csp-npi'
    nm_csp_oli = 'nm-csp-oli'
    nm_csp_pip = 'nm-csp-pip'
    nm_csp_ppm = 'nm-csp-ppm'
    nm_csp_st2 = 'nm-csp-st2'
    nm_csp_xea = 'nm-csp-xea'
    nm_pea_any = 'nm-pea-any'
    nm_pea_ari = 'nm-pea-ari'
    nm_pea_bar = 'nm-pea-bar'
    nm_pea_ccp = 'nm-pea-ccp'
    nm_pea_oli = 'nm-pea-oli'
    nm_pea_pip = 'nm-pea-pip'
    nm_pea_pon = 'nm-pea-pon'
    nm_pea_spa = 'nm-pea-spa'
    nm_pea_sps = 'nm-pea-sps'
    nm_pea_top = 'nm-pea-top'
    nm_pea_vie = 'nm-pea-vie'
    nm_pea_vnm = 'nm-pea-vnm'
    nm_pea_xea = 'nm-pea-xea'
    nm_pea_xwi = 'nm-pea-xwi'
    nm_res_amb = 'nm-res-amb'
    nm_res_ari = 'nm-res-ari'
    nm_res_arx = 'nm-res-arx'
    nm_res_bar = 'nm-res-bar'
    nm_res_bio = 'nm-res-bio'
    nm_res_cha = 'nm-res-cha'
    nm_res_cns = 'nm-res-cns'
    nm_res_cya = 'nm-res-cya'
    nm_res_dyn = 'nm-res-dyn'
    nm_res_gro = 'nm-res-gro'
    nm_res_isd = 'nm-res-isd'
    nm_res_noa = 'nm-res-noa'
    nm_res_oth = 'nm-res-oth'
    nm_res_ros = 'nm-res-ros'
    nm_res_sax = 'nm-res-sax'
    nm_res_sch = 'nm-res-sch'
    nm_res_syb = 'nm-res-syb'
    nm_res_xpl = 'nm-res-xpl'
    nm_shi = 'nm-shi'
    nm_shi_ari = 'nm-shi-ari'
    nm_shi_bar = 'nm-shi-bar'
    nm_shi_gar = 'nm-shi-gar'
    nm_shi_npi = 'nm-shi-npi'
    nm_shi_oli = 'nm-shi-oli'
    nm_shi_pip = 'nm-shi-pip'
    nm_shi_ppm = 'nm-shi-ppm'
    nm_shi_st2 = 'nm-shi-st2'
    nm_shi_xea = 'nm-shi-xea'
    nm_uni_nef = 'nm-uni-nef'
    nm_uni_str = 'nm-uni-str'


class UploadFileSource(str, Enum):

    user = 'user'
    bmrb = 'bmrb'


class OutputFileType(str, Enum):

    compressed = 'compressed'
    json_report = 'json_report'
    nef = 'nef'
    nmr_star = 'nmr-star'
    pdbx = 'pdbx'
    pdf_report = 'pdf_report'
    text_report = 'text_report'


class DeliveryStatusCode(str, Enum):

    failed = 'failed'
    queued = 'queued'
    sent = 'sent'


class WfTaskCode(str, Enum):

    communication = 'communication'
    convert_model = 'convert_model'
    convert_nmr_data = 'convert_nmr_data'
    convert_pdf = 'convert_pdf'
    issue_conversion = 'issue_conversion'
    nef_release = 'nef_release'
    notification = 'notification'
    session_cleanup = 'session_cleanup'
    session_exchange = 'session_exchange'
    session_lock = 'session_lock'


class WfStatusCode(str, Enum):

    aborted = 'aborted'
    completed = 'completed'
    created = 'created'
    failed = 'failed'
    pending = 'pending'
    processing = 'processing'


class Session(Base):

    __tablename__ = 'session'

    processing_site = sa.Column(EnumStr('processing_site_code'), server_default=SERVICE_DOMAIN)
    token = sa.Column(UUID, server_default='uuidv7()', primary_key=True)
    token_admin = sa.Column(UUID, server_default='gen_random_uuid()')
    token_expiry = sa.Column(sa.TIMESTAMP(), nullable=False)
    consented = sa.Column(sa.Boolean(), nullable=False, server_default='FALSE')
    user_id = sa.Column(UUID, sa.ForeignKey('app_user.id'))  # owner (NULL = anonymous)
    client_ip = sa.Column(INET())
    user_agent = sa.Column(sa.Text())
    status = sa.Column(EnumStr('session_status_code'), nullable=False, server_default='created')
    target_depsys = sa.Column(EnumStr('target_depsys_code'), nullable=False, server_default='onedep')
    related_bmrb_id = sa.Column(sa.Integer())
    latest_run_number = sa.Column(sa.Integer(), nullable=False, server_default='0')
    conversion_id = sa.Column(sa.Integer(), unique=True)
    created_at = sa.Column(sa.TIMESTAMP(), server_default=func.now())
    started_at = sa.Column(sa.TIMESTAMP())
    finished_at = sa.Column(sa.TIMESTAMP())
    approved = sa.Column(sa.Boolean(), server_default='FALSE')
    exchanged = sa.Column(sa.Boolean(), server_default='FALSE')
    downloaded = sa.Column(sa.Boolean(), server_default='FALSE')
    help_user_seen_at = sa.Column(sa.TIMESTAMP())


class UploadFile(Base):

    __tablename__ = 'upload_file'

    token = sa.Column(UUID, sa.ForeignKey('session.token'), primary_key=True)
    ordinal = sa.Column(sa.Integer(), primary_key=True)
    conversion_id = sa.Column(sa.Integer(), sa.ForeignKey('session.conversion_id'))
    run_number = sa.Column(sa.Integer(), nullable=False, server_default='1')
    original_name = sa.Column(sa.Text(), nullable=False)
    stored_path = sa.Column(sa.Text(), nullable=False)
    file_size = sa.Column(sa.BigInteger())
    checksum = sa.Column(sa.Text())
    file_type = sa.Column(EnumStr('upload_file_type'))
    selected = sa.Column(sa.Boolean(), nullable=False, server_default='TRUE')
    source = sa.Column(EnumStr('upload_file_source'), nullable=False, server_default='user')
    uploaded_at = sa.Column(sa.TIMESTAMP(), server_default=func.now())


class OutputFile(Base):

    __tablename__ = 'output_file'

    conversion_id = sa.Column(sa.Integer(), sa.ForeignKey('session.conversion_id'), primary_key=True)
    run_number = sa.Column(sa.Integer(), primary_key=True, server_default='1')
    ordinal = sa.Column(sa.Integer(), primary_key=True)
    stored_path = sa.Column(sa.Text(), nullable=False)
    file_size = sa.Column(sa.BigInteger())
    checksum = sa.Column(sa.Text())
    file_type = sa.Column(EnumStr('output_file_type'), nullable=False)
    downloaded = sa.Column(sa.Boolean(), nullable=False, server_default='TRUE')
    downloaded_at = sa.Column(sa.TIMESTAMP(), server_default=func.now())
    client_ip = sa.Column(INET())
    user_agent = sa.Column(sa.Text())


class Notification(Base):

    __tablename__ = 'notification'

    conversion_id = sa.Column(sa.Integer(), sa.ForeignKey('session.conversion_id'), primary_key=True)
    ordinal = sa.Column(sa.Integer(), primary_key=True)
    subject = sa.Column(sa.Text(), nullable=False)
    content = sa.Column(sa.Text(), nullable=False)
    sent_at = sa.Column(sa.TIMESTAMP(), server_default=func.now())
    delivery_status = sa.Column(EnumStr('delivery_status_code'))


class Communication(Base):

    __tablename__ = 'communication'

    conversion_id = sa.Column(sa.Integer(), sa.ForeignKey('session.conversion_id'), primary_key=True)
    ordinal = sa.Column(sa.Integer(), primary_key=True)
    subject = sa.Column(sa.Text(), nullable=False)
    content = sa.Column(sa.Text(), nullable=False)
    email_address = sa.Column(sa.Text(), nullable=False)
    sent_at = sa.Column(sa.TIMESTAMP(), server_default=func.now())
    delivery_status = sa.Column(EnumStr('delivery_status_code'))
    from_admin = sa.Column(sa.Boolean(), nullable=False, server_default='FALSE')
    is_help_desk = sa.Column(sa.Boolean(), nullable=False, server_default='FALSE')


class Workflow(Base):

    __tablename__ = 'workflow'

    conversion_id = sa.Column(sa.Integer(), sa.ForeignKey('session.conversion_id'), primary_key=True)
    run_number = sa.Column(sa.Integer(), primary_key=True, server_default='1')
    ordinal = sa.Column(sa.Integer(), primary_key=True)
    task = sa.Column(EnumStr('wf_task_code'), nullable=False)
    status = sa.Column(EnumStr('wf_status_code'), nullable=False, server_default='created')
    log_path = sa.Column(sa.Text(), nullable=False)
    report_status = sa.Column(sa.Text())
    report_summary = sa.Column(sa.Text())
    created_at = sa.Column(sa.TIMESTAMP(), server_default=func.now())
    started_at = sa.Column(sa.TIMESTAMP())
    finished_at = sa.Column(sa.TIMESTAMP())
    expiry_at = sa.Column(sa.TIMESTAMP())


class UserRoleCode(str, Enum):

    user = 'user'
    annotator = 'annotator'


class AppUser(Base):

    __tablename__ = 'app_user'

    id = sa.Column(UUID, server_default='uuidv7()', primary_key=True)
    email = sa.Column(sa.Text(), nullable=False, unique=True)
    role = sa.Column(EnumStr('user_role_code'), nullable=False, server_default='user')
    totp_secret = sa.Column(sa.Text())
    totp_enrolled = sa.Column(sa.Boolean(), nullable=False, server_default='FALSE')
    disabled = sa.Column(sa.Boolean(), nullable=False, server_default='FALSE')
    created_at = sa.Column(sa.TIMESTAMP(), server_default=func.now())
    last_login_at = sa.Column(sa.TIMESTAMP())


class LoginChallenge(Base):

    __tablename__ = 'login_challenge'

    id = sa.Column(UUID, server_default='uuidv7()', primary_key=True)
    email = sa.Column(sa.Text(), nullable=False)
    token_hash = sa.Column(sa.Text(), nullable=False)
    purpose = sa.Column(sa.Text(), nullable=False, server_default='login')
    created_at = sa.Column(sa.TIMESTAMP(), server_default=func.now())
    expires_at = sa.Column(sa.TIMESTAMP(), nullable=False)
    consumed_at = sa.Column(sa.TIMESTAMP())
    attempts = sa.Column(sa.Integer(), nullable=False, server_default='0')
    claim_token = sa.Column(sa.Text())  # session.token to adopt on verify (pending claim)


class AuthSession(Base):

    __tablename__ = 'auth_session'

    id = sa.Column(sa.Text(), primary_key=True)
    user_id = sa.Column(UUID, sa.ForeignKey('app_user.id', ondelete='CASCADE'), nullable=False)
    csrf_token = sa.Column(sa.Text(), nullable=False)
    totp_ok = sa.Column(sa.Boolean(), nullable=False, server_default='FALSE')
    created_at = sa.Column(sa.TIMESTAMP(), server_default=func.now())
    last_seen_at = sa.Column(sa.TIMESTAMP(), server_default=func.now())
    absolute_expiry = sa.Column(sa.TIMESTAMP(), nullable=False)
    revoked = sa.Column(sa.Boolean(), nullable=False, server_default='FALSE')
    client_ip = sa.Column(INET())
    user_agent = sa.Column(sa.Text())


class AdminAccessAudit(Base):

    __tablename__ = 'admin_access_audit'

    id = sa.Column(UUID, server_default='uuidv7()', primary_key=True)
    annotator_id = sa.Column(UUID, sa.ForeignKey('app_user.id'))
    session_token = sa.Column(UUID)
    conversion_id = sa.Column(sa.Integer())
    action = sa.Column(sa.Text(), nullable=False)
    at = sa.Column(sa.TIMESTAMP(), server_default=func.now())
    client_ip = sa.Column(INET())

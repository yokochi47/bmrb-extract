# This code is generated from postgres/init-service.sql.template
# by omymodels Online (https://archon-omymodels-online.hf.space)
import sqlalchemy as sa
from sqlalchemy.ext.declarative import declarative_base
from enum import Enum
from sqlalchemy.sql import func
from sqlalchemy.dialects.postgresql import UUID, INET

from core.site_config import SERVICE_DOMAIN


Base = declarative_base()


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


class UploadFileType(str, Enum):

    co_cif = 'co-cif'
    co_pdb = 'co-pdb'
    nm_aux_amb = 'nm-aux-amb'
    nm_aux_cha = 'nm-aux-cha'
    nm_aux_gro = 'nm-aux-gro'
    nm_aux_pdb = 'nm-aux-pdb'
    nm_aux_xea = 'nm-aux-xea'
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


class OutputFileType(str, Enum):

    compressed = 'compressed'
    json_report = 'json_report'
    nef = 'nef'
    nmr_star = 'nmr-star'
    pdbx = 'pdbx'
    text_report = 'text_report'


class DeliveryStatusCode(str, Enum):

    failed = 'failed'
    queued = 'queued'
    sent = 'sent'


class WfTaskCode(str, Enum):

    communication = 'communication'
    convert_model = 'convert_model'
    convert_nmr_data = 'convert_nmr_data'
    issue_conversion = 'issue_conversion'
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

    processing_site = sa.Column(sa.Text(), server_default=SERVICE_DOMAIN)
    token = sa.Column(UUID, server_default='uuidv7()', primary_key=True)
    token_admin = sa.Column(UUID, server_default='gen_random_uuid()')
    token_expiry = sa.Column(sa.TIMESTAMP(), nullable=False)
    consented = sa.Column(sa.Boolean(), nullable=False, server_default='FALSE')
    client_ip = sa.Column(INET())
    user_agent = sa.Column(sa.Text())
    status = sa.Column(sa.Enum(SessionStatusCode), nullable=False, server_default='created')
    conversion_id = sa.Column(sa.Integer(), unique=True)
    created_at = sa.Column(sa.TIMESTAMP(), server_default=func.now())
    started_at = sa.Column(sa.TIMESTAMP())
    finished_at = sa.Column(sa.TIMESTAMP())
    approved = sa.Column(sa.Boolean(), server_default='FALSE')
    exchanged = sa.Column(sa.Boolean(), server_default='FALSE')
    downloaded = sa.Column(sa.Boolean(), server_default='FALSE')


class UploadFile(Base):

    __tablename__ = 'upload_file'

    token = sa.Column(UUID, sa.ForeignKey('session.token'), primary_key=True)
    ordinal = sa.Column(sa.Integer(), primary_key=True)
    conversion_id = sa.Column(sa.Integer(), sa.ForeignKey('session.conversion_id'))
    original_name = sa.Column(sa.Text(), nullable=False)
    stored_path = sa.Column(sa.Text(), nullable=False)
    file_size = sa.Column(sa.BigInteger())
    checksum = sa.Column(sa.Text())
    file_type = sa.Column(sa.Text(), nullable=False)
    selected = sa.Column(sa.Boolean(), nullable=False, server_default='TRUE')
    uploaded_at = sa.Column(sa.TIMESTAMP(), server_default=func.now())


class OutputFile(Base):

    __tablename__ = 'output_file'

    conversion_id = sa.Column(sa.Integer(), sa.ForeignKey('session.conversion_id'), primary_key=True)
    ordinal = sa.Column(sa.Integer(), primary_key=True)
    stored_path = sa.Column(sa.Text(), nullable=False)
    file_size = sa.Column(sa.BigInteger())
    checksum = sa.Column(sa.Text())
    file_type = sa.Column(sa.Text(), nullable=False)
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
    delivery_status = sa.Column(sa.Enum(DeliveryStatusCode))


class Communication(Base):

    __tablename__ = 'communication'

    conversion_id = sa.Column(sa.Integer(), sa.ForeignKey('session.conversion_id'), primary_key=True)
    ordinal = sa.Column(sa.Integer(), primary_key=True)
    subject = sa.Column(sa.Text(), nullable=False)
    content = sa.Column(sa.Text(), nullable=False)
    email_address = sa.Column(sa.Text(), nullable=False)
    sent_at = sa.Column(sa.TIMESTAMP(), server_default=func.now())
    delivery_status = sa.Column(sa.Enum(DeliveryStatusCode))


class Workflow(Base):

    __tablename__ = 'workflow'

    conversion_id = sa.Column(sa.Integer(), sa.ForeignKey('session.conversion_id'), primary_key=True)
    ordinal = sa.Column(sa.Integer(), primary_key=True)
    task = sa.Column(sa.Enum(WfTaskCode), nullable=False)
    status = sa.Column(sa.Enum(WfStatusCode), nullable=False, server_default='created')
    log_path = sa.Column(sa.Text(), nullable=False)
    created_at = sa.Column(sa.TIMESTAMP(), server_default=func.now())
    started_at = sa.Column(sa.TIMESTAMP())
    finished_at = sa.Column(sa.TIMESTAMP())
    expiry_at = sa.Column(sa.TIMESTAMP())

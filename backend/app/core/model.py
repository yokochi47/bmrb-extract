# This code is generated from postgres/init-service.sql.template
# by omymodels Online (https://archon-omymodels-online.hf.space)
import sqlalchemy as sa
from sqlalchemy.ext.declarative import declarative_base
from enum import Enum
from sqlalchemy.sql import func
from sqlalchemy.dialects.postgresql import UUID


Base = declarative_base()


class SessionStatusCode(str, Enum):

    completed = 'completed'
    created = 'created'
    expired = 'expired'
    failed = 'failed'
    processing = 'processing'
    uploading = 'uploading'


class UploadFileType(str, Enum):

    co-cif = 'co-cif'
    co-pdb = 'co-pdb'
    nm-aux-amb = 'nm-aux-amb'
    nm-aux-cha = 'nm-aux-cha'
    nm-aux-gro = 'nm-aux-gro'
    nm-aux-pdb = 'nm-aux-pdb'
    nm-aux-xea = 'nm-aux-xea'
    nm-pea-any = 'nm-pea-any'
    nm-pea-ari = 'nm-pea-ari'
    nm-pea-bar = 'nm-pea-bar'
    nm-pea-ccp = 'nm-pea-ccp'
    nm-pea-oli = 'nm-pea-oli'
    nm-pea-pip = 'nm-pea-pip'
    nm-pea-pon = 'nm-pea-pon'
    nm-pea-spa = 'nm-pea-spa'
    nm-pea-sps = 'nm-pea-sps'
    nm-pea-top = 'nm-pea-top'
    nm-pea-vie = 'nm-pea-vie'
    nm-pea-vnm = 'nm-pea-vnm'
    nm-pea-xea = 'nm-pea-xea'
    nm-pea-xwi = 'nm-pea-xwi'
    nm-res-amb = 'nm-res-amb'
    nm-res-ari = 'nm-res-ari'
    nm-res-arx = 'nm-res-arx'
    nm-res-bar = 'nm-res-bar'
    nm-res-bio = 'nm-res-bio'
    nm-res-cha = 'nm-res-cha'
    nm-res-cns = 'nm-res-cns'
    nm-res-cya = 'nm-res-cya'
    nm-res-dyn = 'nm-res-dyn'
    nm-res-gro = 'nm-res-gro'
    nm-res-isd = 'nm-res-isd'
    nm-res-noa = 'nm-res-noa'
    nm-res-oth = 'nm-res-oth'
    nm-res-ros = 'nm-res-ros'
    nm-res-sax = 'nm-res-sax'
    nm-res-sch = 'nm-res-sch'
    nm-res-syb = 'nm-res-syb'
    nm-res-xpl = 'nm-res-xpl'
    nm-shi = 'nm-shi'
    nm-shi-ari = 'nm-shi-ari'
    nm-shi-bar = 'nm-shi-bar'
    nm-shi-gar = 'nm-shi-gar'
    nm-shi-npi = 'nm-shi-npi'
    nm-shi-oli = 'nm-shi-oli'
    nm-shi-pip = 'nm-shi-pip'
    nm-shi-ppm = 'nm-shi-ppm'
    nm-shi-st2 = 'nm-shi-st2'
    nm-shi-xea = 'nm-shi-xea'
    nm-uni-nef = 'nm-uni-nef'
    nm-uni-str = 'nm-uni-str'


class OutputFileType(str, Enum):

    compressed = 'compressed'
    json_report = 'json_report'
    nef = 'nef'
    nmr-star = 'nmr-star'
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

    token = sa.Column(UUID, server_default='uuidv7()', primary_key=True)
    token_admin = sa.Column(UUID, server_default='gen_random_uuid()')
    token_expiry = sa.Column(sa.TIMESTAMP(), nullable=False)
    terms_consent = sa.Column(sa.Boolean(), nullable=False, server_default='FALSE')
    terms_consent_time = sa.Column(sa.TIMESTAMP())
    client_ip = sa.Column(inet())
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

    token = sa.Column(UUID, sa.ForeignKey('session.token'), ondelete="CASCADE", primary_key=True)
    ordinal = sa.Column(sa.Integer(), primary_key=True)
    conversion_id = sa.Column(sa.Integer(), sa.ForeignKey('session.conversion_id'))
    original_name = sa.Column(sa.Text(), nullable=False)
    stored_path = sa.Column(sa.Text(), nullable=False)
    file_size = sa.Column(sa.BigInteger())
    checksum = sa.Column(sa.Text())
    file_type = sa.Column(sa.Enum(UploadFileType), nullable=False)
    selected = sa.Column(sa.Boolean(), nullable=False, server_default='TRUE')
    uploaded_at = sa.Column(sa.TIMESTAMP(), server_default=func.now())


class OutputFile(Base):

    __tablename__ = 'output_file'

    conversion_id = sa.Column(sa.Integer(), sa.ForeignKey('session.conversion_id'), ondelete="CASCADE", primary_key=True)
    ordinal = sa.Column(sa.Integer(), primary_key=True)
    stored_path = sa.Column(sa.Text(), nullable=False)
    file_size = sa.Column(sa.BigInteger())
    checksum = sa.Column(sa.Text())
    file_type = sa.Column(sa.Enum(OutputFileType), nullable=False)
    downloaded = sa.Column(sa.Boolean(), nullable=False, server_default='TRUE')
    downloaded_at = sa.Column(sa.TIMESTAMP(), server_default=func.now())
    client_ip = sa.Column(inet())
    user_agent = sa.Column(sa.Text())


class Notification(Base):

    __tablename__ = 'notification'

    conversion_id = sa.Column(sa.Integer(), sa.ForeignKey('session.conversion_id'), ondelete="CASCADE", primary_key=True)
    ordinal = sa.Column(sa.Integer(), primary_key=True)
    subject = sa.Column(sa.Text(), nullable=False)
    content = sa.Column(sa.Text(), nullable=False)
    sent_at = sa.Column(sa.TIMESTAMP(), server_default=func.now())
    delivery_status = sa.Column(sa.Enum(DeliveryStatusCode))


class Communication(Base):

    __tablename__ = 'communication'

    conversion_id = sa.Column(sa.Integer(), sa.ForeignKey('session.conversion_id'), ondelete="CASCADE", primary_key=True)
    ordinal = sa.Column(sa.Integer(), primary_key=True)
    subject = sa.Column(sa.Text(), nullable=False)
    content = sa.Column(sa.Text(), nullable=False)
    email_address = sa.Column(sa.Text(), nullable=False)
    sent_at = sa.Column(sa.TIMESTAMP(), server_default=func.now())
    delivery_status = sa.Column(sa.Enum(DeliveryStatusCode))


class Workflow(Base):

    __tablename__ = 'workflow'

    conversion_id = sa.Column(sa.Integer(), sa.ForeignKey('session.conversion_id'), ondelete="CASCADE")
    ordinal = sa.Column(sa.Integer())
    task = sa.Column(sa.Enum(WfTaskCode), nullable=False)
    status = sa.Column(sa.Enum(WfStatusCode), nullable=False, server_default='created')
    log_path = sa.Column(sa.Text(), nullable=False)
    created_at = sa.Column(sa.TIMESTAMP(), server_default=func.now())
    started_at = sa.Column(sa.TIMESTAMP())
    finished_at = sa.Column(sa.TIMESTAMP())
    expiry_at = sa.Column(sa.TIMESTAMP())

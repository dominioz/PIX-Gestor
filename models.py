import hashlib
from datetime import datetime
from flask_sqlalchemy import SQLAlchemy

try:
    from zoneinfo import ZoneInfo
except ImportError:
    from backports.zoneinfo import ZoneInfo

# Instância única do SQLAlchemy que será inicializada no app.py com db.init_app(app)
db = SQLAlchemy()


class Usuario(db.Model):
    __tablename__ = 'usuarios'

    id = db.Column(db.Integer, primary_key=True)
    nome = db.Column(db.String(100), nullable=False)
    email = db.Column(db.String(150), unique=True, nullable=False)
    senha_hash = db.Column(db.String(255), nullable=False)
    nivel = db.Column(db.Enum('admin', 'consumidor'), default='consumidor', nullable=False)
    api_key = db.Column(db.String(64), unique=True, nullable=False)
    chave_pix_padrao = db.Column(db.String(100), nullable=True)
    cidade_padrao = db.Column(db.String(15), default='SAO PAULO')
    descricao_padrao = db.Column(db.String(25), default='PAGAMENTO')
    limite_mensal = db.Column(db.Integer, default=1000, nullable=False)
    
    # CORRIGIDO: Alterado de db.Boolean para db.Integer (0 = Inativo, 1 = Ativo, 2 = Senha Temporária)
    ativo = db.Column(db.Integer, default=1, nullable=False)
    
    criado_em = db.Column(db.DateTime, default=datetime.utcnow)
    atualizado_em = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    logs = db.relationship('LogGeracao', backref='usuario', lazy=True, cascade="all, delete-orphan")

    def consumiu_cota_no_mes(self) -> int:
        """Retorna a quantidade de QR Codes ÚNICOS (por MD5) gerados no mês vigente."""
        agora = datetime.utcnow()
        inicio_mes = datetime(agora.year, agora.month, 1)
        
        unicos_no_mes = db.session.query(LogGeracao.payload_md5)\
            .filter(LogGeracao.usuario_id == self.id)\
            .filter(LogGeracao.criado_em >= inicio_mes)\
            .distinct().count()
            
        return unicos_no_mes


class LogGeracao(db.Model):
    __tablename__ = 'logs_geracao'

    id = db.Column(db.BigInteger, primary_key=True)
    usuario_id = db.Column(db.Integer, db.ForeignKey('usuarios.id'), nullable=False)
    payload_md5 = db.Column(db.String(32), nullable=False)
    valor = db.Column(db.Numeric(10, 2), nullable=False)
    tipo = db.Column(db.Enum('qrcode', 'payload'), nullable=False)
    criado_em = db.Column(db.DateTime, default=datetime.utcnow)

    @staticmethod
    def gerar_md5(payload_str: str) -> str:
        """Gera o hash MD5 da string Pix Copia e Cola."""
        return hashlib.md5(payload_str.encode('utf-8')).hexdigest()


class Cobranca(db.Model):
    __tablename__ = "cobrancas"

    id = db.Column(db.Integer, primary_key=True)
    usuario_id = db.Column(db.Integer, db.ForeignKey("usuarios.id"), nullable=False)
    nome_cliente = db.Column(db.String(100), nullable=False)
    email_cliente = db.Column(db.String(150), nullable=False)
    descricao = db.Column(db.String(25), nullable=False)
    valor = db.Column(db.Numeric(10, 2), nullable=False)
    status = db.Column(db.Enum("pendente", "pago", "cancelado"), default="pendente")
    payload_pix = db.Column(db.Text, nullable=False)
    criado_em = db.Column(db.DateTime, default=lambda: datetime.now(ZoneInfo("UTC")))
    pago_em = db.Column(db.DateTime, nullable=True)
    lido = db.Column(db.Boolean, default=False)
    lido_em = db.Column(db.DateTime, nullable=True)
    
    usuario = db.relationship("Usuario", backref=db.backref("cobrancas", lazy=True))


class HistoricoChavePix(db.Model):
    __tablename__ = 'historico_chave_pix'
    id = db.Column(db.Integer, primary_key=True)
    usuario_id = db.Column(db.Integer, db.ForeignKey('usuarios.id'), nullable=False)
    chave_pix = db.Column(db.String(100), nullable=False)
    criado_em = db.Column(db.DateTime, default=lambda: datetime.now(ZoneInfo("UTC")))
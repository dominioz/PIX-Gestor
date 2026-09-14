import base64
import hashlib
import os
import secrets
import unicodedata
from datetime import datetime, timedelta
from io import BytesIO
from urllib.parse import quote_plus

try:
    from zoneinfo import ZoneInfo
except ImportError:
    from backports.zoneinfo import ZoneInfo

import qrcode
from flask import (
    Flask,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    session,
    url_for,
)
from flask_cors import CORS
from flask_mail import Mail, Message
from PIL import Image, ImageDraw, ImageFont
from sqlalchemy import func
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash, generate_password_hash

# Importa os modelos SQLAlchemy (incluindo o novo HistoricoChavePix)
from models import Cobranca, LogGeracao, Usuario, HistoricoChavePix, db

app = Flask(__name__)

# Fuso horário do Brasil (Horário de Brasília)
FUSO_SP = ZoneInfo("America/Sao_Paulo")

# =============================================================================
# CHAVE SECRETA OBRIGATÓRIA PARA SESSÕES E MENSAGENS FLASH DO FLASK
# =============================================================================
app.secret_key = os.environ.get("minha_chave_secreta_flask", secrets.token_hex(32))

# -----------------------------------------------------------------------------
# CONFIGURAÇÃO DO BANCO DE DADOS MYSQL
# -----------------------------------------------------------------------------
DB_USER = "dbuser"
DB_PASS = "dbpass"
DB_HOST = "hostmysql"
DB_PORT = "3306"
DB_NAME = "base_pix"

senha_encoded = quote_plus(DB_PASS)

app.config["SQLALCHEMY_DATABASE_URI"] = (
    f"mysql+pymysql://{DB_USER}:{senha_encoded}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
)
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {
    "pool_recycle": 280,
    "pool_pre_ping": True,
}

# -----------------------------------------------------------------------------
# CONFIGURAÇÃO DO SERVIDOR DE E-MAIL (SMTP)
# -----------------------------------------------------------------------------
app.config["MAIL_SERVER"] = "smtp.mailserver"
app.config["MAIL_PORT"] = 587
app.config["MAIL_USE_TLS"] = False
app.config["MAIL_USE_SSL"] = True
app.config["MAIL_USERNAME"] = "username@mailserver"
app.config["MAIL_PASSWORD"] = "password"
app.config["MAIL_DEFAULT_SENDER"] = ("PIX Gestor", "username@mailserver")

mail = Mail(app)
db.init_app(app)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
CORS(app)

# -----------------------------------------------------------------------------
# FILTROS E FUNÇÕES AUXILIARES DE FORMATAÇÃO
# -----------------------------------------------------------------------------
@app.template_filter("format_datetime_br")
def format_datetime_br(value):
    if not value:
        return ""
    if value.tzinfo is None:
        value = value.replace(tzinfo=ZoneInfo("UTC"))
    data_br = value.astimezone(FUSO_SP)
    return data_br.strftime("%d/%m/%Y %H:%M:%S")

def formatar_chave_pix(chave: str) -> str:
    chave = str(chave).strip()
    if "@" in chave:
        return chave.lower()

    apenas_numeros = "".join(c for c in chave if c.isdigit())
    if len(apenas_numeros) in [11, 14] and not chave.startswith("+"):
        return apenas_numeros
    if len(apenas_numeros) in [10, 11] and (chave.startswith("+") or len(apenas_numeros) == 11):
        if not chave.startswith("+55"):
            return f"+55{apenas_numeros}"
        return f"+{apenas_numeros}"

    return chave.lower()

def tratar_valor(val) -> float:
    if isinstance(val, (int, float)):
        return float(val)
    val_str = str(val).strip()
    if "," in val_str:
        val_str = val_str.replace(".", "").replace(",", ".")
    return float(val_str)

def limpar_texto(texto: str) -> str:
    if not texto:
        return ""
    texto_sem_acento = "".join(
        c for c in unicodedata.normalize("NFD", str(texto))
        if unicodedata.category(c) != "Mn"
    ).upper()
    texto_limpo = "".join(c for c in texto_sem_acento if c.isalnum() or c.isspace())
    return " ".join(texto_limpo.split())

def crc16(payload: str) -> str:
    polinomio = 0x1021
    resultado = 0xFFFF
    for char in payload:
        resultado ^= ord(char) << 8
        for _ in range(8):
            if resultado & 0x8000:
                resultado = (resultado << 1) ^ polinomio
            else:
                resultado <<= 1
            resultado &= 0xFFFF
    return format(resultado, "04X")

def tag(id_tag: str, value: str) -> str:
    return f"{id_tag}{len(value):02d}{value}"

def gerar_payload_pix(chave_pix: str, nome_recebedor: str, valor, descricao: str = "***", cidade: str = "SAO PAULO") -> str:
    chave_pix = formatar_chave_pix(chave_pix)
    nome_recebedor = limpar_texto(nome_recebedor)[:25]
    cidade = limpar_texto(cidade)[:15]

    if descricao and str(descricao).strip() and str(descricao).strip() != "***":
        txid_limpo = "".join(c for c in unicodedata.normalize("NFD", str(descricao)) if c.isalnum()).upper()[:25]
        txid_formatado = txid_limpo if txid_limpo else "***"
    else:
        txid_formatado = "***"

    valor_num = tratar_valor(valor)
    valor_formatado = f"{valor_num:.2f}"

    gui = "BR.GOV.BCB.PIX"
    campo26 = tag("26", tag("00", gui) + tag("01", chave_pix))
    campo62 = tag("62", tag("05", txid_formatado))

    payload_partes = [
        "000201", campo26, "52040000", "5303986", tag("54", valor_formatado),
        "5802BR", tag("59", nome_recebedor), tag("60", cidade), campo62, "6304",
    ]

    payload_completo = "".join(payload_partes)
    crc = crc16(payload_completo)
    return payload_completo + crc

def formatar_moeda_br(valor) -> str:
    return f"{float(valor):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")

def enviar_email_cobranca(cobranca):
    img = qrcode.make(cobranca.payload_pix)
    buffer = BytesIO()
    img.save(buffer, format="PNG")
    buffer.seek(0)

    pixel_url = url_for('cobranca_pixel', id_cob=cobranca.id, _external=True)
    qr_url = url_for(
        'qrcode_img',
        api_key=cobranca.usuario.api_key,
        valor=cobranca.valor,
        descricao=cobranca.descricao,
        _external=True
    )

    nome_remetente = f"{cobranca.usuario.nome} - PIX Gestor"
    email_remetente = "pix-qrcode@brz.dev.br"
    valor_br = formatar_moeda_br(cobranca.valor)

    msg = Message(
        subject=f"Solicitação de Pagamento PIX - R$ {valor_br}",
        recipients=[cobranca.email_cliente],
        sender=(nome_remetente, email_remetente),
        reply_to=cobranca.usuario.email,
    )

    msg.html = f"""
    <div style="font-family: Arial, sans-serif; max-width: 600px; margin: 0 auto; border: 1px solid #e0e0e0; border-radius: 8px; padding: 20px;">
        <h2 style="color: #0d6efd;">Solicitação de Pagamento do PIX Gestor</h2>
        <p>Olá, <strong>{cobranca.nome_cliente}</strong>,</p>
        <p>Você recebeu uma solicitação de pagamento emitida por <strong>{cobranca.usuario.nome}</strong>.</p>
        <table style="width: 100%; margin: 20px 0; border-collapse: collapse;">
            <tr>
                <td style="padding: 8px; background: #f8f9fa;"><strong>Descrição:</strong></td>
                <td style="padding: 8px; background: #f8f9fa;">{cobranca.descricao}</td>
            </tr>
            <tr>
                <td style="padding: 8px;"><strong>Valor:</strong></td>
                <td style="padding: 8px; color: #198754; font-size: 18px;"><strong>R$ {valor_br}</strong></td>
            </tr>
        </table>
        <div style="text-align: center; margin: 25px 0;">
            <p style="font-weight: bold; margin-bottom: 10px;">Escaneie o QR Code abaixo para pagar:</p>
            <img src="{qr_url}" alt="QR Code PIX" style="width: 220px; height: 220px; border: 1px solid #ddd; padding: 10px; border-radius: 8px; background: #fff;" />
        </div>
        <p><strong>PIX Copia e Cola:</strong></p>
        <div style="background: #f1f1f1; padding: 10px; word-break: break-all; font-family: monospace; border-radius: 4px; font-size: 13px;">
            {cobranca.payload_pix}
        </div>
        <p style="margin-top: 20px; font-size: 14px;">Utilize o QR Code acima, o código Copia e Cola ou a imagem em anexo neste e-mail para efetuar o pagamento.</p>
        <hr style="border: none; border-top: 1px solid #eee; margin: 20px 0;">
        <p style="font-size: 12px; color: #888; text-align: center;">Mensagem enviada automaticamente pelo PIX Gestor.</p>
        <img src="{pixel_url}" width="1" height="1" style="display:none;" alt="" />
    </div>
    """
    msg.attach(
        filename=f"qrcode_pix_{cobranca.id}.png",
        content_type="image/png",
        data=buffer.getvalue(),
    )
    mail.send(msg)

def gerar_imagem_erro(mensagem: str) -> BytesIO:
    largura, altura = 300, 300
    img = Image.new("RGB", (largura, altura), color="#1a1c23")
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle([(10, 10), (largura - 10, altura - 10)], radius=12, outline="#ff5a7a", width=3)

    try:
        font_titulo = ImageFont.truetype("DejaVuSans-Bold.ttf", 16)
        font_corpo = ImageFont.truetype("DejaVuSans.ttf", 12)
    except IOError:
        font_titulo = ImageFont.load_default()
        font_corpo = ImageFont.load_default()

    draw.text((largura / 2, 45), "ATENÇÃO", fill="#ff5a7a", font=font_titulo, anchor="mm")
    palavras = mensagem.split()
    linhas = []
    linha_atual = ""
    for palavra in palavras:
        teste = f"{linha_atual} {palavra}".strip()
        if len(teste) * 7.5 > (largura - 40):
            linhas.append(linha_atual)
            linha_atual = palavra
        else:
            linha_atual = teste
    if linha_atual:
        linhas.append(linha_atual)

    y_text = 120
    for linha in linhas:
        draw.text((largura / 2, y_text), linha, fill="#ffffff", font=font_corpo, anchor="mm")
        y_text += 20

    buffer = BytesIO()
    img.save(buffer, format="PNG")
    buffer.seek(0)
    return buffer

def processar_requisicao_pix(tipo_requisicao: str):
    api_key = request.args.get("api_key")
    if not api_key:
        return None, "Chave de API (api_key) ausente.", 401
    usuario = Usuario.query.filter_by(api_key=api_key, ativo=True).first()
    if not usuario:
        return None, "API Key inválida ou conta inativa.", 403
    valor = request.args.get("valor")
    if not valor:
        return None, "Parâmetro 'valor' é obrigatório.", 400
    chave_pix = usuario.chave_pix_padrao
    if not chave_pix:
        return None, "Chave PIX não encontrada no cadastro do usuário.", 400

    nome_recebedor = usuario.nome
    cidade = usuario.cidade_padrao or "SAO PAULO"
    descricao = request.args.get("descricao") or usuario.descricao_padrao or "***"

    payload = gerar_payload_pix(chave_pix=chave_pix, nome_recebedor=nome_recebedor, valor=valor, descricao=descricao, cidade=cidade)
    payload_md5 = LogGeracao.gerar_md5(payload)
    agora_utc = datetime.now(ZoneInfo("UTC"))
    inicio_mes_utc = datetime(agora_utc.year, agora_utc.month, 1, tzinfo=ZoneInfo("UTC"))

    log_existente = (
        LogGeracao.query.filter_by(usuario_id=usuario.id, payload_md5=payload_md5)
        .filter(LogGeracao.criado_em >= inicio_mes_utc)
        .first()
    )

    if not log_existente:
        cota_usada = usuario.consumiu_cota_no_mes()
        if cota_usada >= usuario.limite_mensal:
            return None, "Limite mensal de QR Codes atingido.", 429

    novo_log = LogGeracao(usuario_id=usuario.id, payload_md5=payload_md5, valor=tratar_valor(valor), tipo=tipo_requisicao)
    db.session.add(novo_log)
    db.session.commit()
    return payload, None, 200

# -----------------------------------------------------------------------------
# ROTAS DO PAINEL WEB
# -----------------------------------------------------------------------------
@app.route("/", methods=["GET"])
def index():
    usuario_id = session.get("usuario_id")
    usuario_logado = db.session.get(Usuario, usuario_id) if usuario_id else None

    cota_usada = 0
    logs_usuario = []
    lista_usuarios = []
    cobrancas = []

    if usuario_logado:
        cota_usada = usuario_logado.consumiu_cota_no_mes()
        subquery = (
            db.session.query(
                LogGeracao.payload_md5,
                func.max(LogGeracao.criado_em).label("max_criado_em"),
            )
            .filter(LogGeracao.usuario_id == usuario_logado.id)
            .group_by(LogGeracao.payload_md5)
            .subquery()
        )
        logs_usuario = (
            LogGeracao.query.join(
                subquery,
                (LogGeracao.payload_md5 == subquery.c.payload_md5)
                & (LogGeracao.criado_em == subquery.c.max_criado_em),
            )
            .filter(LogGeracao.usuario_id == usuario_logado.id)
            .order_by(LogGeracao.criado_em.desc())
            .limit(50)
            .all()
        )

        if usuario_logado.nivel == "admin":
            cobrancas = Cobranca.query.order_by(Cobranca.criado_em.desc()).all()
            todos = Usuario.query.all()
            for u in todos:
                lista_usuarios.append({"usuario": u, "cota_usada": u.consumiu_cota_no_mes()})
        else:
            cobrancas = Cobranca.query.filter_by(usuario_id=usuario_logado.id).order_by(Cobranca.criado_em.desc()).all()

    return render_template(
        "index.html",
        usuario_logado=usuario_logado,
        cota_usada=cota_usada,
        logs_usuario=logs_usuario,
        lista_usuarios=lista_usuarios,
        cobrancas=cobrancas,
    )


# -----------------------------------------------------------------------------
# ROTA DE RECUPERÇÃO DE SENHA
# -----------------------------------------------------------------------------
@app.route("/recuperar-senha", methods=["GET", "POST"])
def recuperar_senha():
    if request.method == "POST":
        email = request.form.get("email")
        usuario = Usuario.query.filter_by(email=email).first()

        if usuario:
            # Gera uma nova senha temporária aleatória
            nova_senha_temp = secrets.token_urlsafe(6)
            usuario.senha_hash = generate_password_hash(nova_senha_temp)
            
            # Altera o status para 2 (indica que precisa alterar a senha no próximo login)
            usuario.ativo = 2
            db.session.commit()

            # Envia o e-mail informando a senha temporária e o link de acesso
            try:
                msg = Message(
                    subject="Recuperação de Senha - PIX Gestor",
                    recipients=[email],
                    body=f"Olá {usuario.nome},\n\nRecebemos uma solicitação de recuperação de senha.\nSua senha temporária é: {nova_senha_temp}\n\nFaça login no sistema para alterá-la imediatamente.\n\nAtenciosamente,\nEquipe PIX Gestor"
                )
                mail.send(msg)
                flash("Uma nova senha temporária foi enviada para o seu e-mail.", "success")
            except Exception as e:
                flash(f"Erro ao enviar o e-mail de recuperação: {str(e)}", "danger")
        else:
            flash("Se o e-mail estiver cadastrado, uma nova senha temporária foi enviada.", "success")

        return redirect(url_for("index"))

    return render_template("recuperar_senha.html")
# -----------------------------------------------------------------------------
# NOVAS ROTAS DE CADASTRO E ATIVAÇÃO
# -----------------------------------------------------------------------------
@app.route("/cadastro", methods=["GET", "POST"])
def cadastro():
    if request.method == "POST":
        nome = request.form.get("nome")
        email = request.form.get("email")
        senha = request.form.get("senha")
        chave_pix = request.form.get("chave_pix")

        if Usuario.query.filter_by(email=email).first():
            flash("Este e-mail já está em uso.", "danger")
            return redirect(url_for("cadastro"))

        # TRAVA ANTIFRAUDE: Verifica histórico
        historico = HistoricoChavePix.query.filter_by(chave_pix=chave_pix).all()
        for h in historico:
            user_antigo = db.session.get(Usuario, h.usuario_id)
            if user_antigo and user_antigo.consumiu_cota_no_mes() >= user_antigo.limite_mensal:
                flash(f"Operação negada: A chave PIX informada já atingiu o limite de {user_antigo.limite_mensal} QRs e não pode ser vinculada a uma nova conta.", "danger")
                return redirect(url_for("cadastro"))

        api_key = secrets.token_hex(32)
        novo_usuario = Usuario(
            nome=nome,
            email=email,
            senha_hash=generate_password_hash(senha),
            nivel="consumidor",
            limite_mensal=5,
            chave_pix_padrao=chave_pix,
            api_key=api_key,
            ativo=False
        )
        
        db.session.add(novo_usuario)
        db.session.commit()

        db.session.add(HistoricoChavePix(usuario_id=novo_usuario.id, chave_pix=chave_pix))
        db.session.commit()

        link_ativacao = url_for('ativar_conta', api_key=api_key, _external=True)
        msg = Message(
            subject="Ativação de Conta - PIX Gestor",
            recipients=[email],
            body=f"Olá {nome},\n\nPara ativar sua conta e liberar seus limites, clique no link abaixo:\n\n{link_ativacao}\n\nAtenciosamente,\nEquipe PIX Gestor"
        )
        mail.send(msg)

        flash("Pré-cadastro realizado! Verifique seu e-mail para ativar a conta.", "success")
        return redirect(url_for("index"))

    return render_template("cadastro.html")

@app.route("/ativar/<api_key>")
def ativar_conta(api_key):
    usuario = Usuario.query.filter_by(api_key=api_key).first()
    if usuario:
        if not usuario.ativo:
            usuario.ativo = True
            db.session.commit()
            flash("Conta ativada com sucesso! Você já pode acessar o sistema.", "success")
        else:
            flash("Sua conta já encontra-se ativa.", "info")
    else:
        flash("Link de ativação inválido ou expirado.", "danger")
    return redirect(url_for("index"))

# -----------------------------------------------------------------------------
# ROTAS DE GERENCIAMENTO DE COBRANÇAS
# -----------------------------------------------------------------------------
PIXEL_GIF = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII=")

@app.route("/cobranca/pixel/<int:id_cob>.png", methods=["GET"])
def cobranca_pixel(id_cob):
    cob = db.session.get(Cobranca, id_cob)
    if cob and not cob.lido:
        cob.lido = True
        cob.lido_em = datetime.now(ZoneInfo("UTC"))
        db.session.commit()
    response = send_file(BytesIO(PIXEL_GIF), mimetype="image/png")
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response

@app.route("/cobranca/nova", methods=["POST"])
def cobranca_nova():
    usuario_id = session.get("usuario_id")
    if not usuario_id:
        return redirect(url_for("index"))
    usuario = db.session.get(Usuario, usuario_id)
    if not usuario.chave_pix_padrao:
        flash("Cadastre sua chave PIX antes de enviar cobranças.", "warning")
        return redirect(url_for("index"))

    nome_cliente = request.form.get("nome_cliente")
    email_cliente = request.form.get("email_cliente")
    descricao = request.form.get("descricao")
    valor = request.form.get("valor")

    payload = gerar_payload_pix(chave_pix=usuario.chave_pix_padrao, nome_recebedor=usuario.nome, valor=valor, descricao=descricao, cidade=usuario.cidade_padrao or "SAO PAULO")

    nova_cob = Cobranca(usuario_id=usuario.id, nome_cliente=nome_cliente, email_cliente=email_cliente, descricao=descricao, valor=tratar_valor(valor), status="pendente", payload_pix=payload)
    db.session.add(nova_cob)
    db.session.commit()

    try:
        enviar_email_cobranca(nova_cob)
        flash("Cobrança criada e e-mail enviado com sucesso!", "success")
    except Exception as e:
        flash(f"Cobrança salva, mas houve uma falha ao enviar o e-mail: {str(e)}", "warning")
    return redirect(url_for("index"))

@app.route("/cobranca/criar", methods=["POST"])
def cobranca_criar_api():
    dados = request.get_json(silent=True) or request.form
    api_key = dados.get("api_key") or request.args.get("api_key")
    if not api_key:
        return jsonify({"status": "erro", "mensagem": "Chave de API ausente."}), 401
    usuario = Usuario.query.filter_by(api_key=api_key, ativo=True).first()
    if not usuario:
        return jsonify({"status": "erro", "mensagem": "API Key inválida ou conta inativa."}), 403
    if not usuario.chave_pix_padrao:
        return jsonify({"status": "erro", "mensagem": "Usuário não possui Chave PIX cadastrada."}), 400

    nome_cliente = dados.get("nome_cliente")
    email_cliente = dados.get("email_cliente")
    descricao = dados.get("descricao") or usuario.descricao_padrao or "***"
    valor = dados.get("valor")

    if not nome_cliente or not email_cliente or not valor:
        return jsonify({"status": "erro", "mensagem": "Campos obrigatórios ausentes."}), 400

    try:
        valor_num = tratar_valor(valor)
    except:
        return jsonify({"status": "erro", "mensagem": "Formato de valor inválido."}), 400

    payload = gerar_payload_pix(chave_pix=usuario.chave_pix_padrao, nome_recebedor=usuario.nome, valor=valor_num, descricao=descricao, cidade=usuario.cidade_padrao or "SAO PAULO")
    nova_cob = Cobranca(usuario_id=usuario.id, nome_cliente=nome_cliente, email_cliente=email_cliente, descricao=descricao, valor=valor_num, status="pendente", payload_pix=payload)
    db.session.add(nova_cob)
    db.session.commit()

    email_enviado = True
    erro_email = None
    try:
        enviar_email_cobranca(nova_cob)
    except Exception as e:
        email_enviado = False
        erro_email = str(e)

    qr_code_url = url_for('qrcode_img', api_key=usuario.api_key, valor=valor_num, descricao=descricao, _external=True)
    return jsonify({
        "status": "sucesso",
        "cobranca": {
            "id": nova_cob.id,
            "valor_formatado": f"R$ {formatar_moeda_br(nova_cob.valor)}",
            "pix_copia_cola": nova_cob.payload_pix,
            "qr_code_url": qr_code_url,
            "email_enviado": email_enviado,
            "erro_email": erro_email
        }
    }), 201
    
@app.route("/cobranca/status/<int:id_cob>", methods=["POST"])
def cobranca_status(id_cob):
    usuario_id = session.get("usuario_id")
    if not usuario_id:
        return redirect(url_for("index"))
    cob = db.session.get(Cobranca, id_cob)
    if cob:
        cob.status = request.form.get("status")
        if cob.status == "pago":
            cob.pago_em = datetime.now(ZoneInfo("UTC"))
        db.session.commit()
        flash(f"Status da cobrança #{cob.id} alterado para {cob.status.upper()}.", "info")
    return redirect(url_for("index"))

@app.route("/cobranca/reenviar/<int:id_cob>", methods=["POST"])
def cobranca_reenviar(id_cob):
    usuario_id = session.get("usuario_id")
    if not usuario_id:
        return redirect(url_for("index"))
    cob = db.session.get(Cobranca, id_cob)
    if cob:
        try:
            cob.lido = False
            cob.lido_em = None
            db.session.commit()
            enviar_email_cobranca(cob)
            flash(f"E-mail reenviado para {cob.email_cliente}!", "success")
        except Exception as e:
            flash(f"Erro ao reenviar: {str(e)}", "danger")
    return redirect(url_for("index"))

@app.route("/cobranca/editar/<int:id_cob>", methods=["POST"])
def cobranca_editar(id_cob):
    usuario_id = session.get("usuario_id")
    if not usuario_id:
        return redirect(url_for("index"))
    cob = db.session.get(Cobranca, id_cob)
    if cob:
        usuario = cob.usuario
        cob.nome_cliente = request.form.get("nome_cliente")
        cob.email_cliente = request.form.get("email_cliente")
        cob.descricao = request.form.get("descricao")
        cob.valor = tratar_valor(request.form.get("valor"))
        cob.payload_pix = gerar_payload_pix(chave_pix=usuario.chave_pix_padrao, nome_recebedor=usuario.nome, valor=cob.valor, descricao=cob.descricao, cidade=usuario.cidade_padrao or "SAO PAULO")
        db.session.commit()
        flash(f"Cobrança #{cob.id} atualizada com sucesso!", "success")
    return redirect(url_for("index"))

# -----------------------------------------------------------------------------
# ROTAS DE AUTENTICAÇÃO E CONTA
# -----------------------------------------------------------------------------
@app.route("/login", methods=["POST"])
def login():
    email = request.form.get("email")
    senha = request.form.get("senha")
    usuario = Usuario.query.filter_by(email=email).first()

    if usuario and check_password_hash(usuario.senha_hash, senha):
        # Se usuario.ativo for False (0) ou Inativo
        if usuario.ativo is False or usuario.ativo == 0:
            flash("Sua conta não foi ativada. Verifique seu e-mail.", "danger")
            return redirect(url_for("index"))
            
        session["usuario_id"] = usuario.id
        flash(f"Bem-vindo, {usuario.nome}!", "success")
    else:
        flash("E-mail ou senha inválidos.", "danger")
    return redirect(url_for("index"))

@app.route("/logout")
def logout():
    session.pop("usuario_id", None)
    flash("Sessão encerrada com sucesso.", "info")
    return redirect(url_for("index"))

@app.route("/salvar-perfil", methods=["POST"])
def salvar_perfil():
    usuario_id = session.get("usuario_id")
    if not usuario_id:
        return redirect(url_for("index"))

    usuario = db.session.get(Usuario, usuario_id)
    nova_chave = request.form.get("chave_pix_padrao")
    
    if nova_chave and nova_chave != usuario.chave_pix_padrao:
        historico = HistoricoChavePix.query.filter_by(chave_pix=nova_chave).all()
        for h in historico:
            user_antigo = db.session.get(Usuario, h.usuario_id)
            if user_antigo and user_antigo.id != usuario.id and user_antigo.consumiu_cota_no_mes() >= user_antigo.limite_mensal:
                flash("Operação negada: Esta chave PIX já atingiu o limite de uso em outra conta.", "danger")
                return redirect(url_for("index"))
        
        usuario.chave_pix_padrao = nova_chave
        db.session.add(HistoricoChavePix(usuario_id=usuario.id, chave_pix=nova_chave))

    cidade = request.form.get("cidade_padrao")
    if cidade:
        usuario.cidade_padrao = cidade

    descricao = request.form.get("descricao_padrao")
    if descricao:
        usuario.descricao_padrao = descricao

    db.session.commit()
    flash("Suas configurações foram atualizadas com sucesso!", "success")
    return redirect(url_for("index"))

@app.route("/regerar-apikey", methods=["POST"])
def regerar_apikey():
    usuario_id = session.get("usuario_id")
    if not usuario_id:
        return redirect(url_for("index"))
    usuario = db.session.get(Usuario, usuario_id)
    usuario.api_key = secrets.token_hex(32)
    db.session.commit()
    flash("Nova API Key gerada com sucesso!", "success")
    return redirect(url_for("index"))

@app.route("/alterar-minha-senha", methods=["POST"])
def alterar_minha_senha():
    usuario_id = session.get("usuario_id")
    if not usuario_id:
        return redirect(url_for("index"))
    usuario = db.session.get(Usuario, usuario_id)
    senha_atual = request.form.get("senha_atual")
    nova_senha = request.form.get("nova_senha")
    confirmar_senha = request.form.get("confirmar_senha")

    # Se o usuário estiver no modo temporário (ativo == 2), podemos opcionalmente pular a checagem da senha atual 
    # ou exigir que ele digite a senha temporária que recebeu por e-mail no campo "senha_atual".
    if usuario.ativo != 2 and not check_password_hash(usuario.senha_hash, senha_atual):
        flash("Senha atual incorreta.", "danger")
        return redirect(url_for("index"))

    if nova_senha != confirmar_senha:
        flash("Nova senha e confirmação não conferem.", "danger")
        return redirect(url_for("index"))

    usuario.senha_hash = generate_password_hash(nova_senha)
    usuario.ativo = 1  # Retorna o status para ativo normal
    db.session.commit()
    flash("Senha alterada com sucesso!", "success")
    return redirect(url_for("index"))

# -----------------------------------------------------------------------------
# ROTAS EXCLUSIVAS DO ADMINISTRADOR
# -----------------------------------------------------------------------------
@app.route("/admin/criar-usuario", methods=["POST"])
def admin_criar_usuario():
    usuario_id = session.get("usuario_id")
    admin = db.session.get(Usuario, usuario_id) if usuario_id else None

    if not admin or admin.nivel != "admin":
        flash("Acesso não autorizado.", "danger")
        return redirect(url_for("index"))

    email = request.form.get("email")
    if Usuario.query.filter_by(email=email).first():
        flash("Já existe um usuário cadastrado com este e-mail.", "danger")
        return redirect(url_for("index"))

    novo_usuario = Usuario(
        nome=request.form.get("nome"),
        email=email,
        senha_hash=generate_password_hash(request.form.get("senha")),
        nivel=request.form.get("nivel", "consumidor"),
        limite_mensal=int(request.form.get("limite_mensal", 1000)),
        chave_pix_padrao=request.form.get("chave_pix_padrao"),
        api_key=secrets.token_hex(32),
        ativo=True,
    )
    db.session.add(novo_usuario)
    db.session.commit()
    flash("Novo usuário cadastrado com sucesso!", "success")
    return redirect(url_for("index"))

@app.route("/admin/atualizar-limite/<int:id_user>", methods=["POST"])
def admin_atualizar_limite(id_user):
    usuario_id = session.get("usuario_id")
    admin = db.session.get(Usuario, usuario_id) if usuario_id else None

    if not admin or admin.nivel != "admin":
        flash("Acesso não autorizado.", "danger")
        return redirect(url_for("index"))

    u = db.session.get(Usuario, id_user)
    if u:
        u.limite_mensal = int(request.form.get("limite_mensal", 1000))
        db.session.commit()
        flash(f"Limite do usuário {u.nome} atualizado!", "success")
    return redirect(url_for("index"))

@app.route("/admin/alternar-status/<int:id_user>", methods=["POST"])
def admin_alternar_status(id_user):
    usuario_id = session.get("usuario_id")
    admin = db.session.get(Usuario, usuario_id) if usuario_id else None
    if not admin or admin.nivel != "admin":
        flash("Acesso não autorizado.", "danger")
        return redirect(url_for("index"))

    u = db.session.get(Usuario, id_user)
    if u and u.id != admin.id:
        u.ativo = not u.ativo
        db.session.commit()
        flash(f"Status do usuário {u.nome} alterado com sucesso!", "info")
    return redirect(url_for("index"))

@app.route("/admin/alterar-senha-usuario/<int:id_user>", methods=["POST"])
def admin_alterar_senha_usuario(id_user):
    usuario_id = session.get("usuario_id")
    admin = db.session.get(Usuario, usuario_id) if usuario_id else None
    if not admin or admin.nivel != "admin":
        flash("Acesso não autorizado.", "danger")
        return redirect(url_for("index"))

    u = db.session.get(Usuario, id_user)
    nova_senha = request.form.get("nova_senha")
    if u and nova_senha:
        u.senha_hash = generate_password_hash(nova_senha)
        db.session.commit()
        flash(f"Senha do usuário {u.nome} alterada com sucesso!", "success")
    else:
        flash("Usuário não encontrado ou senha inválida.", "danger")
    return redirect(url_for("index"))

# -----------------------------------------------------------------------------
# ROTAS DA API PIX
# -----------------------------------------------------------------------------
@app.route("/qrcode", methods=["GET"])
def qrcode_img():
    try:
        payload, erro_msg, status_code = processar_requisicao_pix("qrcode")
        if erro_msg:
            buffer_erro = gerar_imagem_erro(erro_msg)
            return send_file(buffer_erro, mimetype="image/png"), status_code
        img = qrcode.make(payload)
        buffer = BytesIO()
        img.save(buffer, format="PNG")
        buffer.seek(0)
        return send_file(buffer, mimetype="image/png")
    except Exception:
        buffer_erro = gerar_imagem_erro("Erro ao gerar QR Code")
        return send_file(buffer_erro, mimetype="image/png"), 500

@app.route("/payload", methods=["GET"])
def pix_payload():
    try:
        payload, erro_msg, status_code = processar_requisicao_pix("payload")
        if erro_msg:
            return jsonify({"status": "erro", "mensagem": erro_msg}), status_code
        return jsonify({"status": "sucesso", "pix_copia_cola": payload})
    except Exception:
        return jsonify({"status": "erro", "mensagem": "Erro interno do servidor"}), 500

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=8088)
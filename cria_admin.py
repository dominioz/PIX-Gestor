import secrets
from werkzeug.security import generate_password_hash
from pix import app, db, Usuario  # Certifique-se que 'app' é o arquivo principal

with app.app_context():
    email = "admin@seudominio.com.br"
    senha_plana = "123mudar"

    # Remove o usuário anterior se existir para evitar duplicidade
    usuario_antigo = Usuario.query.filter_by(email=email).first()
    if usuario_antigo:
        db.session.delete(usuario_antigo)
        db.session.commit()

    # Cria o novo usuário usando os métodos da própria aplicação
    novo_admin = Usuario(
        nome="Seu Nome",
        email=email,
        senha_hash=generate_password_hash(senha_plana),
        nivel="admin",
        limite_mensal=10000,
        chave_pix_padrao="pix@chave.com.br",
        cidade_padrao="SAO PAULO",
        descricao_padrao="PAGAMENTO",
        api_key=secrets.token_hex(32),
        ativo=True,
    )

    db.session.add(novo_admin)
    db.session.commit()
    print("✅ Usuário Admin criado/redefinido com sucesso!")
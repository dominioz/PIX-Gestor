# PIX Gestor (API & Painel Web)

## 1. Introdução
O **PIX Gestor** é uma aplicação web e API desenvolvida em Python utilizando o microframework Flask. O objetivo do sistema é facilitar a geração de códigos PIX (QR Code e formato Copia e Cola) de forma estática, além de gerenciar limites mensais de geração por usuário e enviar cobranças completas por e-mail.
Poderá visualizar este projeto online no site [https://](https://pix-qrcode.brz.dev.br/)

## 2. Explicação Básica
O sistema é composto por duas frentes integradas:
*   **Painel de Gestão (Web):** Uma interface em que usuários (consumidores e administradores) podem se cadastrar, fazer login, cadastrar suas chaves PIX e disparar e-mails de cobrança para clientes contendo um QR Code.
*   **API de Geração:** Endpoints REST que permitem a integração do PIX Gestor em sistemas de terceiros através de uma `api_key` exclusiva de cada conta.

A aplicação utiliza um banco de dados MySQL para armazenar dados de cadastro, registrar um log MD5 de cada carga PIX gerada (garantindo o controle da cota do plano) e manter o histórico das cobranças pagas ou pendentes.

## 3. Pré-requisitos
Para hospedar e executar esta aplicação, você precisará de:
*   **Python 3.8+**
*   **Servidor MySQL** (Recomendado versão 8.0+)
*   **Nginx** (Para servir como Proxy Reverso)
*   **Servidor SMTP** (Para o disparo dos e-mails automáticos)

## 4. Instalação e Configuração

**Passo 1: Preparar o Banco de Dados**
Crie um banco de dados MySQL e importe o arquivo de estrutura SQL do projeto:
```bash
mysql -u seu_usuario -p -e "CREATE DATABASE base_pix CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;"
mysql -u seu_usuario -p base_pix < mysql.sql
```

**Passo 2: Configurar o Ambiente Python**
Clone o repositório, crie um ambiente virtual e instale as dependências necessárias:
```bash
git clone https://github.com/seu-usuario/pix-gestor.git
cd pix-gestor
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

**Passo 3: Editar as Configurações**
Abra o arquivo `pix.py` e configure suas credenciais de banco de dados e servidor de e-mail:
```python
DB_USER = "seu_usuario"
DB_PASS = "sua_senha_mysql"
DB_HOST = "localhost"
DB_NAME = "base_pix"

# Configurações de SMTP
app.config["MAIL_SERVER"] = "smtp.seuservidor.com"
app.config["MAIL_USERNAME"] = "seu_email@dominio.com"
app.config["MAIL_PASSWORD"] = "sua_senha_email"
```

**Passo 4: Criar o Usuário Administrador**
Para criar o seu primeiro usuário de acesso ao painel, execute o script de provisionamento:
```bash
python cria_admin.py
```
Isso criará o usuário `admin@seudominio.com.br` com a senha `123mudar` e um limite de 10.000 requisições mensais.

**Passo 5: Iniciar o Servidor Flask Manualmente (Teste)**
Para verificar se tudo está funcionando, inicie a aplicação manualmente (rodará na porta `8088`):
```bash
python pix.py
```
Pressione `Ctrl+C` para encerrar.

**Passo 6: Criar um Serviço no Linux (Systemd)**
Para manter a aplicação rodando em segundo plano de forma confiável no seu servidor Linux, crie um serviço `systemd`.

1. Crie o arquivo de serviço:
```bash
sudo nano /etc/systemd/system/pix-gestor.service
```

2. Cole o conteúdo abaixo (ajuste o caminho `/caminho/para/o/pix-gestor` para o diretório real onde você clonou o projeto e defina o `User` apropriado):
```ini
[Unit]
Description=PIX Gestor - Flask API e Painel
After=network.target mysql.service

[Service]
User=root
Group=www-data
WorkingDirectory=/caminho/para/o/pix-gestor
Environment="PATH=/caminho/para/o/pix-gestor/venv/bin"
ExecStart=/caminho/para/o/pix-gestor/venv/bin/python pix.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

3. Recarregue os serviços, ative o PIX Gestor para iniciar no boot e inicie o serviço:
```bash
sudo systemctl daemon-reload
sudo systemctl enable pix-gestor
sudo systemctl start pix-gestor
```

4. Verifique o status para garantir que está rodando sem erros:
```bash
sudo systemctl status pix-gestor
```

## 5. Como Utilizar

### Pelo Painel Web
1. Acesse o sistema pelo navegador, faça o login e navegue até "Minhas Configurações".
2. Defina o seu tipo de Chave PIX (CPF, CNPJ, E-mail, Celular ou Aleatória) e salve.
3. Na seção "Gerenciador de Cobranças", clique em **+ Nova Cobrança** para disparar um e-mail de cobrança para seu cliente contendo o QR Code.
4. Monitore os acessos na interface, alterando o status da cobrança para "Pago" ou "Cancelado" conforme necessário.

### Pela API Rest
Utilize a sua `api_key` gerada no painel para integrar a geração PIX nos seus scripts:

*   **Obter QR Code (Imagem PNG):**
    `GET /qrcode?api_key=SUA_KEY&valor=15.50&descricao=PEDIDO01`
*   **Obter Payload (PIX Copia e Cola via JSON):**
    `GET /payload?api_key=SUA_KEY&valor=15.50`
*   **Disparar Cobrança e-mail via API:**
    `POST /cobranca/criar` enviando um JSON com `nome_cliente`, `email_cliente`, `valor` e `api_key`.

## 6. Como Publicar com Proxy Reverso (Nginx)

Para expor o serviço (que está rodando na porta 8088 pelo serviço do systemd) de forma profissional para a internet, configure o Nginx como proxy reverso.

Crie um novo arquivo de configuração do Nginx (ex: `/etc/nginx/sites-available/pix_gestor`):

```nginx
server {
    listen 80;
    server_name pix.seudominio.com.br;

    # Opcional: Logs de acesso para auditoria
    access_log /var/log/nginx/pix_gestor_access.log;
    error_log /var/log/nginx/pix_gestor_error.log;

    location / {
        # Encaminha o tráfego para a aplicação local gerenciada pelo systemd
        proxy_pass http://127.0.0.1:8088;
        
        # Cabeçalhos importantes para a aplicação e para o Werkzeug ProxyFix
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        
        proxy_redirect off;
    }
}
```

**Ativando o Proxy no Nginx:**
```bash
sudo ln -s /etc/nginx/sites-available/pix_gestor /etc/nginx/sites-enabled/
sudo nginx -t
sudo systemctl reload nginx
```
*(Dica: Recomendamos executar `certbot --nginx` posteriormente para instalar um certificado SSL gratuito).*

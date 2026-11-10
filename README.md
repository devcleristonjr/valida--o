# PLEITOS — cadastro e controle de audiências

Aplicação web SERIN com a página pública existente preservada, uma API Flask e um painel administrativo separado. As inscrições, audiências, credenciais e presenças ficam no banco do servidor; imagens são gravadas fora da pasta pública. PostgreSQL é o banco recomendado/esperado em produção. SQLite é apenas uma opção para desenvolvimento e testes.

## Arquitetura

- `Cadastro Audiência.html`: interface pública original (layout, marca e campos preservados). O servidor retira a implementação legada de LocalStorage ao servir a página e injeta `static/js/public.js`. A audiência, data e horário são definidos pela equipe na área administrativa; no cadastro público, escolha uma audiência ativa para preencher esses campos.
- `pleitos/routes/`: API pública e rotas administrativas protegidas.
- `pleitos/models.py`: audiências, participantes, logs, administradores e desafios de consulta.
- `pleitos/services.py`: cifragem, armazenamento privado de fotos, credenciais e check-in atômico.
- `pleitos/templates/admin.html`: interface administrativa em `/admin`.
- `static/`: estilos e clientes JavaScript.
- PostgreSQL (`DATABASE_URL`) e armazenamento privado configurável (`UPLOAD_FOLDER`).

## Executar localmente no Windows

Use Python 3.11+ (testado com Python 3.13). Node.js/npm só são necessários para recompilar o leitor QR local.

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
Copy-Item .env.example .env
```

O bundle do leitor QR já vem no projeto. Para atualizá-lo após alterar `package.json`, execute `npm ci` e `npm run build:qr`.

Edite `.env` com valores próprios. Gere três chaves independentes (não use os exemplos nem as publique):

```powershell
python -c "import secrets; print(secrets.token_urlsafe(48))"
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
python -c "import secrets; print(secrets.token_hex(32))"
```

Use a primeira saída em `SECRET_KEY`, a segunda em `APP_ENCRYPTION_KEY` e a terceira em `DATA_HASH_KEY`. Defina também `ADMIN_USERNAME` e `ADMIN_PASSWORD` (mínimo 12 caracteres). Opcionalmente, configure `RECEPTION_USERNAME` e `RECEPTION_PASSWORD` para uma conta de recepção com permissão restrita ao check-in e painel agregado. Para o SQLite local, pode-se trocar `DATABASE_URL` por `sqlite:///pleitos.db`. Para PostgreSQL:

```text
DATABASE_URL=postgresql+psycopg://USUARIO:SENHA@HOST:5432/NOME_BANCO
```

Inicialize o esquema versionado e crie a conta administrativa configurada:

```powershell
flask --app app db upgrade
flask --app app init-db
```

Inicie para desenvolvimento:

```powershell
flask --app app run --host 127.0.0.1 --port 5000
```

Abra `http://127.0.0.1:5000/` para escolher a área, `/cadastro` para inscrição e `/admin` para administração. A área administrativa exige login. O acesso à câmera exige contexto seguro: `localhost` funciona para testes locais; celulares em rede precisam acessar por HTTPS.

No painel em telas pequenas, a lista de participantes vira cartões para evitar rolagem horizontal, os controles mantêm alvos de toque maiores e a leitura por câmera evita abrir o teclado após um QR reconhecido. No iPhone, use Safari em uma URL HTTPS e permita o acesso à câmera; se a permissão for negada, confira os ajustes do Safari/iOS ou digite o código manualmente.

## Publicação

1. Provisionar PostgreSQL gerenciado, usuário de banco com privilégio mínimo, backup criptografado e armazenamento persistente privado para `UPLOAD_FOLDER`.
2. Configurar as chaves do ambiente, `DATABASE_URL`, conta inicial, `SESSION_COOKIE_SECURE=1` e, para consulta segura da credencial por CPF, SMTP/TLS (`SMTP_HOST`, `SMTP_PORT`, credenciais e remetente).
3. Executar `flask --app app db upgrade` e `flask --app app init-db` na instalação inicial. Para alterações de esquema, gerar e revisar `flask --app app db migrate -m "descrição"` e aplicar com `flask --app app db upgrade`.
4. Publicar atrás de um proxy HTTPS (por exemplo IIS/Nginx/serviço de plataforma) e iniciar o WSGI com `waitress-serve --listen=127.0.0.1:8000 app:app`. Não usar o servidor de desenvolvimento Flask em produção. Configure limite de requisições para login, inscrição e solicitação de códigos no proxy/WAF.
5. Testar restauração de backup do banco **e** das fotos, monitorar saúde em `/api/health`, e fazer deploy inicial restrito até completar a revisão institucional de privacidade.

O acesso a fotos, cadastros detalhados, histórico e exportações exige sessão administrativa. A conta de recepção só acessa validação de entrada e indicadores agregados. Senhas são armazenadas com scrypt; sessão usa cookie HttpOnly/SameSite e token CSRF nas mutações; os campos CPF, telefone, e-mail e o token da credencial são cifrados com Fernet. O banco contém HMACs para pesquisa/duplicidade. Fotos não são expostas pela rota estática. Check-in usa atualização condicional atômica no banco, impedindo segunda liberação concorrente.

## API

| Método | Rota | Acesso | Descrição |
|---|---|---|---|
| GET | `/api/health` | Público | Saúde da aplicação |
| GET | `/api/audiences` | Público | Audiências abertas para inscrição |
| POST | `/api/participants` | Público | Cadastro multipart com foto e ciência de privacidade |
| POST | `/api/credentials/qr` | Código válido | PNG QR da credencial, validado pelo servidor |
| POST | `/api/credentials/lookup` | Público | Inicia recuperação com resposta não enumerável |
| POST | `/api/credentials/lookup/verify` | Código por e-mail | Confirma identidade e retorna a própria credencial |
| POST | `/api/admin/login` | Público | Login administrativo |
| GET/POST | `/api/admin/audiences` | Admin | Lista/cria audiência |
| PATCH | `/api/admin/audiences/<id>` | Admin + CSRF | Ativa/desativa inscrição |
| GET/PATCH | `/api/admin/participants` e `/<id>` | Admin + CSRF em escrita | Pesquisa e edição |
| GET | `/api/admin/participants/<id>/photo` | Admin | Fotografia protegida |
| GET | `/api/admin/participants/<id>/credential` | Admin | Consulta de credencial |
| POST | `/api/admin/check-in` | Admin + CSRF | Validação QR ou leitor USB/Bluetooth |
| POST | `/api/admin/participants/<id>/manual-check-in` | Admin + CSRF | Liberação justificada |
| POST | `/api/admin/participants/<id>/cancel` | Admin + CSRF | Cancelamento |
| DELETE | `/api/admin/participants/<id>` | Admin + CSRF | Exclusão/anonymização operacional irreversível |
| GET | `/api/admin/dashboard` | Admin | Indicadores e entradas recentes |
| GET | `/api/admin/history` | Admin | Histórico de acessos e operações |
| GET | `/api/admin/export?format=csv\|xlsx\|pdf` | Admin | Lista filtrada de presença |

Todas as rotas administrativas de escrita exigem `X-CSRF-Token` retornado pelo login. Credenciais QR são aleatórias, persistidas cifradas e verificadas no servidor. A consulta pública por CPF não revela existência de cadastro; o resgate exige código enviado ao e-mail já registrado. Sem SMTP, a inscrição funciona, mas o resgate público fica indisponível e a equipe autenticada pode consultar a credencial.

## Persistência e estrutura do banco

- `audiences`: audiência e horário, com restrição de unicidade.
- `participants`: inscrição vinculada à audiência; unicidade de CPF por audiência; dados de contato e CPF cifrados; fotografia privada; credencial cifrada e hash único; consentimento/versionamento; estado e presença.
- `access_logs`: entradas válidas, duplicadas e tentativas inválidas, sem guardar código inválido em claro.
- `audit_logs`: operações administrativas.
- `admins`: hash de senha, papel (admin/recepção) e estado da conta.
- `lookup_challenges`: hashes de códigos de e-mail com expiração e limite de tentativas.

## Privacidade e itens necessários antes de produção

O código não escolhe prazo de retenção nem fundamento jurídico em nome da SERIN. A instituição precisa aprovar: aviso/base legal e canal do encarregado, prazo de retenção de inscrições/fotos/logs, tratamento de pedidos dos titulares, responsáveis e fluxo de resposta a incidentes. Até essa decisão, não há exclusão automática; administradores autorizados podem cancelar ou excluir um registro com justificativa auditada. É necessária configuração real de PostgreSQL, HTTPS, SMTP, credencial administrativa e armazenamento/backup privado; nenhum desses serviços/segredos estava presente na pasta recebida.

A selfie é apenas fotografia para identificação visual; não há reconhecimento biométrico automatizado. O consentimento/ciência e a versão do aviso ficam associados ao cadastro. O acesso remoto por celular requer HTTPS válido e política de câmera do navegador. O teste automatizado não substitui validação física em Android/iPhone, configuração de proxy, teste de carga, avaliação jurídica LGPD ou exercício de restauração.

## Testes

```powershell
python -m unittest discover -s tests -v
node --check static/js/public.js
node --check static/js/admin.js
```

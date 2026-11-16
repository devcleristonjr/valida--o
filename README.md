# PLEITOS — cadastro e controle de audiências e eventos

Aplicação web SERIN com fluxos públicos separados para audiências e eventos, uma API Flask e um painel administrativo. Inscrições, credenciais e presenças ficam no banco do servidor; imagens são gravadas fora da pasta pública. PostgreSQL é o banco recomendado/esperado em produção. SQLite é apenas uma opção para desenvolvimento e testes.

## Arquitetura

- `Cadastro Audiência.html`: interface pública original (layout, marca e campos preservados). O servidor retira a implementação legada de LocalStorage ao servir a página e injeta `static/js/public.js`. A audiência, data e horário são definidos pela equipe na área administrativa; no cadastro público, escolha uma audiência ativa para preencher esses campos.
- `/evento`: formulário público independente para inscrição em eventos, emissão de credencial QR e recuperação por código enviado ao e-mail cadastrado.
- `pleitos/routes/`: API pública e rotas administrativas protegidas.
- `pleitos/models.py`: audiências, eventos, participantes independentes por fluxo, convites de cadastro, logs, administradores e desafios de consulta.
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

Abra `http://127.0.0.1:5000/` para escolher a área, `/cadastro` para inscrição em audiência, `/evento` para inscrição em evento e `/admin` para administração. A área administrativa exige login. O acesso à câmera exige contexto seguro: `localhost` funciona para testes locais; celulares em rede precisam acessar por HTTPS.

No painel em telas pequenas, a lista de participantes vira cartões para evitar rolagem horizontal, os controles mantêm alvos de toque maiores e a leitura por câmera evita abrir o teclado após um QR reconhecido. No iPhone, use Safari em uma URL HTTPS e permita o acesso à câmera; se a permissão for negada, confira os ajustes do Safari/iOS ou digite o código manualmente.

## Operação de audiências e eventos

Cada atividade pode ser preparada, aberta, encerrada, cancelada ou arquivada e pode ter capacidade definida (campo vazio significa sem limite). Quando as vagas acabam, novas inscrições válidas entram na lista de espera em ordem de chegada; ao liberar uma vaga, a primeira pessoa é promovida automaticamente e recebe uma nova confirmação por e-mail. A credencial de quem ainda aguarda vaga não pode ser usada para check-in. O cancelamento da atividade avisa inscritos confirmados e pessoas na lista de espera e impede novas entradas.

Novos cadastros exigem e-mail. Confirmações, avisos de lista de espera e promoções são enviados logo após a inscrição/operação. Lembretes são enfileirados para 7 dias e 1 dia antes da atividade. Configure SMTP no ambiente (`SMTP_HOST`, `SMTP_PORT`, `SMTP_USERNAME`, `SMTP_PASSWORD`, `SMTP_FROM` e `SMTP_USE_TLS`). Se o envio falhar, o cadastro não é perdido: o e-mail permanece pendente para nova tentativa, e o painel administrativo permite processar a fila manualmente.

Administradores podem gerar convites individuais de cadastro no painel. O link expira em 7 dias por padrão (`REGISTRATION_INVITE_DAYS`), contém um token aleatório e nunca inclui o CPF. No formulário, a pessoa escolhe uma audiência ou evento aberto; a inscrição continua sujeita à regra de CPF único daquela atividade. O token é armazenado somente como HMAC, e o CPF vinculado é cifrado. Se já houver convite ativo, o painel exige confirmação explícita antes de emitir outro link e invalidar o anterior. A geração e a validação de convites têm limites de requisições por endereço de origem; mantenha também a proteção de taxa no proxy/WAF.

Em produção, agende diariamente no Agendador de Tarefas do Windows (ou cron do servidor) a execução de `flask --app app pleitos-process-notifications`, no diretório do projeto e com o ambiente virtual/deploy configurado. O comando é idempotente para lembretes já enfileirados. Use `ACTIVITY_TIMEZONE` para definir o fuso de operação (padrão `America/Bahia`). A retenção automática de dados permanece desativada até aprovação institucional da SERIN.

## Publicação

1. Provisionar PostgreSQL gerenciado, usuário de banco com privilégio mínimo, backup criptografado e armazenamento persistente privado para `UPLOAD_FOLDER`.
2. Configurar as chaves do ambiente, `DATABASE_URL`, conta inicial, `SESSION_COOKIE_SECURE=1` e SMTP/TLS para confirmações, lembretes e consulta segura de credenciais.
3. Executar `flask --app app db upgrade` e `flask --app app init-db` na instalação inicial. Para alterações de esquema, gerar e revisar `flask --app app db migrate -m "descrição"` e aplicar com `flask --app app db upgrade`.
4. Publicar atrás de um proxy HTTPS (por exemplo IIS/Nginx/serviço de plataforma) e iniciar o WSGI com `waitress-serve --listen=127.0.0.1:8000 app:app`. Não usar o servidor de desenvolvimento Flask em produção. Configure limite de requisições para login, inscrição e solicitação de códigos no proxy/WAF.
5. Testar restauração de backup do banco **e** das fotos, monitorar saúde em `/api/health`, e fazer deploy inicial restrito até completar a revisão institucional de privacidade.

O acesso a fotos, cadastros detalhados, histórico e exportações exige sessão administrativa. A conta de recepção só acessa validação de entrada e indicadores agregados. Senhas são armazenadas com scrypt; sessão usa cookie HttpOnly/SameSite e token CSRF nas mutações; CPF, telefone, e-mail, Instagram e tokens de credencial são cifrados com Fernet. O banco contém HMACs para pesquisa/duplicidade. Fotos não são expostas pela rota estática. Check-in usa atualização condicional atômica no banco, impedindo segunda liberação concorrente.

## API

| Método | Rota | Acesso | Descrição |
|---|---|---|---|
| GET | `/api/health` | Público | Saúde da aplicação |
| GET | `/api/audiences` | Público | Audiências abertas para inscrição |
| POST | `/api/participants` | Público | Cadastro multipart com foto e ciência de privacidade |
| GET | `/api/events` | Público | Eventos e situação das inscrições |
| POST | `/api/event-participants` | Público | Cadastro multipart independente para evento |
| POST | `/api/event-credentials/lookup` | Público | Inicia recuperação de credencial de evento, sem revelar cadastro |
| POST | `/api/event-credentials/lookup/verify` | Código por e-mail | Confirma identidade e retorna a credencial do evento |
| POST | `/api/credentials/qr` | Código válido | PNG QR da credencial, validado pelo servidor |
| POST | `/api/credentials/lookup` | Público | Inicia recuperação com resposta não enumerável |
| POST | `/api/credentials/lookup/verify` | Código por e-mail | Confirma identidade e retorna a própria credencial |
| POST | `/api/admin/login` | Público | Login administrativo |
| POST | `/api/admin/convites` | Admin/organizador + CSRF | Gera convite de cadastro; substituição de convite ativo exige confirmação explícita |
| GET | `/api/cadastro/<token>` | Token válido | Valida convite e retorna o CPF vinculado para preenchimento automático |
| POST | `/api/cadastro/<token>` | Token válido | Cria a inscrição na atividade escolhida e consome o convite na mesma transação |
| GET | `/cadastro/<token>` | Público | Tela de cadastro por convite; valida o token antes de exibir o formulário |
| GET/POST | `/api/admin/audiences` | Admin | Lista/cria audiência |
| PATCH | `/api/admin/audiences/<id>` | Admin + CSRF | Ativa/desativa inscrição |
| GET/POST | `/api/admin/events` | Admin | Lista/cria eventos |
| PATCH | `/api/admin/events/<id>` | Admin + CSRF | Edita evento ou abre/encerra inscrições |
| GET | `/api/admin/event-participants?event_id=<id>` | Admin | Pesquisa inscritos e situação de presença |
| GET | `/api/admin/events/<id>/dashboard` | Admin | Indicadores de presença do evento |
| GET | `/api/admin/events/<id>/export` | Admin | Exporta a lista de presença do evento em CSV |
| GET | `/api/admin/notifications` | Admin | Consulta e-mails pendentes e com falha |
| POST | `/api/admin/notifications/process` | Admin + CSRF | Enfileira lembretes devidos e tenta enviar e-mails pendentes |
| GET | `/api/admin/event-participants/<id>/credential` | Admin | Consulta credencial de evento |
| POST | `/api/admin/event-participants/<id>/cancel` | Admin + CSRF | Cancela credencial do evento |
| DELETE | `/api/admin/event-participants/<id>` | Admin + CSRF | Exclui dados do participante do evento |
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

O cadastro por convite ignora qualquer CPF enviado no payload e usa o CPF cifrado associado ao hash do token. Token inválido, expirado, usado ou invalidado recebe a mesma resposta genérica. A tela do convite desativa cache e envio de `Referer`; cadastros e CPFs continuam sujeitos às proteções HTTPS de produção.

## Persistência e estrutura do banco

- `audiences`: audiência e horário, com restrição de unicidade.
- `events`: evento, data, horários, local e estado das inscrições.
- `participants`: inscrição vinculada à audiência; unicidade de CPF por audiência; dados de contato e CPF cifrados; fotografia privada; credencial cifrada e hash único; consentimento/versionamento; estado e presença.
- `event_participants`: inscrições independentes, com unicidade de CPF por evento, dados pessoais cifrados, fotografia privada, credencial e presença próprias.
- `access_logs`: entradas válidas, duplicadas e tentativas inválidas, sem guardar código inválido em claro.
- `audit_logs`: operações administrativas.
- `admins`: hash de senha, papel (admin/organizador/recepção) e estado da conta; organizadores têm acesso restrito à emissão de convites.
- `lookup_challenges`: hashes de códigos de e-mail com expiração e limite de tentativas.
- `registration_invites`: CPF cifrado e HMAC para pesquisa, hash do token, validade, uso/invalidação e administrador emissor; o token puro nunca é persistido.
- `email_notifications`: fila com destinatário e conteúdo cifrados, tentativas, estado e erro de envio; lembretes não são duplicados para a mesma inscrição.

## Privacidade e itens necessários antes de produção

O código não escolhe prazo de retenção nem fundamento jurídico em nome da SERIN. A instituição precisa aprovar: aviso/base legal e canal do encarregado, prazo de retenção de inscrições/fotos/logs, tratamento de pedidos dos titulares, responsáveis e fluxo de resposta a incidentes. Até essa decisão, não há exclusão automática; administradores autorizados podem cancelar ou excluir um registro com justificativa auditada. É necessária configuração real de PostgreSQL, HTTPS, SMTP, credencial administrativa e armazenamento/backup privado; nenhum desses serviços/segredos estava presente na pasta recebida.

A selfie é apenas fotografia para identificação visual; não há reconhecimento biométrico automatizado. O consentimento/ciência e a versão do aviso ficam associados ao cadastro. O acesso remoto por celular requer HTTPS válido e política de câmera do navegador. O teste automatizado não substitui validação física em Android/iPhone, configuração de proxy, teste de carga, avaliação jurídica LGPD ou exercício de restauração.

## Testes

```powershell
python -m unittest discover -s tests -v
node --check static/js/public.js
node --check static/js/admin.js
node --check static/js/invite-registration.js
```

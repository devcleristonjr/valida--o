import os
from pathlib import Path

from dotenv import load_dotenv
from cryptography.fernet import Fernet
from flask import (
    Flask,
    current_app,
    jsonify,
    make_response,
    render_template,
    request,
    send_from_directory,
)

from .extensions import db


def create_app(test_config=None):
    load_dotenv()
    app = Flask(__name__, instance_relative_config=True, static_folder=None)
    app.config.from_mapping(
        SECRET_KEY=os.getenv("SECRET_KEY"),
        APP_ENV=os.getenv("APP_ENV", "development"),
        SQLALCHEMY_DATABASE_URI=os.getenv("DATABASE_URL", "sqlite:///pleitos.db"),
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
        SQLALCHEMY_ENGINE_OPTIONS={"pool_pre_ping": True},
        APP_ENCRYPTION_KEY=os.getenv("APP_ENCRYPTION_KEY"),
        DATA_HASH_KEY=os.getenv("DATA_HASH_KEY"),
        UPLOAD_FOLDER=os.getenv("UPLOAD_FOLDER", "private-uploads"),
        MAX_CONTENT_LENGTH=int(os.getenv("MAX_CONTENT_LENGTH", "5242880")),
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=os.getenv("SESSION_COOKIE_SECURE", "0") == "1",
        PERMANENT_SESSION_LIFETIME=1800,
        PRIVACY_NOTICE_VERSION=os.getenv("PRIVACY_NOTICE_VERSION", "1"),
        ADMIN_USERNAME=os.getenv("ADMIN_USERNAME"),
        ADMIN_PASSWORD=os.getenv("ADMIN_PASSWORD"),
        RECEPTION_USERNAME=os.getenv("RECEPTION_USERNAME"),
        RECEPTION_PASSWORD=os.getenv("RECEPTION_PASSWORD"),
        SMTP_HOST=os.getenv("SMTP_HOST"),
        SMTP_PORT=os.getenv("SMTP_PORT", "587"),
        SMTP_USERNAME=os.getenv("SMTP_USERNAME"),
        SMTP_PASSWORD=os.getenv("SMTP_PASSWORD"),
        SMTP_FROM=os.getenv("SMTP_FROM"),
        SMTP_USE_TLS=os.getenv("SMTP_USE_TLS", "1") == "1",
    )
    if test_config:
        app.config.update(test_config)
    if not app.config["SECRET_KEY"] or len(app.config["SECRET_KEY"]) < 32:
        raise RuntimeError("SECRET_KEY precisa ter ao menos 32 caracteres aleatórios.")
    if not app.config["APP_ENCRYPTION_KEY"]:
        raise RuntimeError("APP_ENCRYPTION_KEY precisa ser definida no ambiente.")
    if not app.config["DATA_HASH_KEY"] or len(app.config["DATA_HASH_KEY"]) < 32:
        raise RuntimeError("DATA_HASH_KEY precisa ser uma chave separada com ao menos 32 caracteres.")
    try:
        Fernet(app.config["APP_ENCRYPTION_KEY"].encode("ascii"))
    except (ValueError, UnicodeEncodeError) as error:
        raise RuntimeError("APP_ENCRYPTION_KEY precisa ser uma chave Fernet válida.") from error
    if app.config["SQLALCHEMY_DATABASE_URI"].startswith("postgres://"):
        app.config["SQLALCHEMY_DATABASE_URI"] = app.config[
            "SQLALCHEMY_DATABASE_URI"
        ].replace("postgres://", "postgresql+psycopg://", 1)
    if app.config["APP_ENV"] == "production":
        if not app.config["SQLALCHEMY_DATABASE_URI"].startswith("postgresql"):
            raise RuntimeError("Produção exige PostgreSQL em DATABASE_URL.")
        if not app.config["SESSION_COOKIE_SECURE"]:
            raise RuntimeError("Produção exige SESSION_COOKIE_SECURE=1 e HTTPS.")

    Path(app.instance_path).mkdir(parents=True, exist_ok=True)
    upload_folder = Path(app.config["UPLOAD_FOLDER"])
    if not upload_folder.is_absolute():
        upload_folder = Path(app.instance_path) / upload_folder
    app.config["UPLOAD_FOLDER"] = str(upload_folder.resolve())
    upload_folder.mkdir(parents=True, exist_ok=True)
    db.init_app(app)
    from flask_migrate import Migrate

    Migrate(app, db)

    from .routes.admin import admin_bp
    from .routes.public import public_bp
    from .models import Admin

    app.register_blueprint(public_bp)
    app.register_blueprint(admin_bp)

    @app.after_request
    def secure_response(response):
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; base-uri 'self'; object-src 'none'; "
            "script-src 'self' 'unsafe-inline' https://cdnjs.cloudflare.com; "
            "style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; "
            "connect-src 'self' https://servicodados.ibge.gov.br; "
            "media-src 'self' blob:; frame-ancestors 'none'; form-action 'self'",
        )
        if request.path.startswith("/api/") or request.path.startswith("/admin"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/")
    def home():
        return render_template("home.html")

    @app.get("/cadastro")
    def public_app():
        html_path = Path(app.root_path).parent / "Cadastro Audiência.html"
        html = html_path.read_text(encoding="utf-8")
        html = html.replace("Validação facial", "Fotografia para credencial")
        html = html.replace(
            "</head>",
            '<link rel="stylesheet" href="/static/css/public-layout.css"></head>',
        )
        html = html.replace(
            "</div></header>",
            '</div><a class="btn home-link" href="/">← Início</a></header>',
            1,
        )
        html = html.replace(
            '<script src="https://cdnjs.cloudflare.com/ajax/libs/qrcodejs/1.0.0/qrcode.min.js"></script>',
            "",
        )
        legacy_marker = "const KEY='pleitos_access_v1'"
        marker_index = html.find(legacy_marker)
        if marker_index < 0:
            current_app.logger.error("Não foi possível localizar o JavaScript legado do PLEITOS.")
            return jsonify({"error": "A interface pública está incompleta."}), 500
        script_start = html.rfind("<script>", 0, marker_index)
        script_end = html.find("</script>", marker_index)
        if script_start < 0 or script_end < 0:
            return jsonify({"error": "Não foi possível preparar a interface pública."}), 500
        script_end += len("</script>")
        html = (
            html[:script_start]
            + '<script src="/static/js/public.js" defer></script>'
            + html[script_end:]
        )
        response = make_response(html)
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/admin")
    def admin_app():
        return render_template("admin.html")

    @app.get("/static/<path:filename>")
    def static_files(filename):
        return send_from_directory(Path(app.root_path).parent / "static", filename)

    @app.get("/api/health")
    def health():
        return jsonify({"status": "ok"})

    @app.cli.command("init-db")
    def init_db():
        """Create tables and optionally bootstrap the configured first administrator."""
        db.create_all()
        username = app.config.get("ADMIN_USERNAME")
        password = app.config.get("ADMIN_PASSWORD")
        if username and password:
            if len(password) < 12:
                raise RuntimeError("ADMIN_PASSWORD deve ter pelo menos 12 caracteres.")
            if not Admin.query.filter_by(username=username).first():
                db.session.add(Admin.create(username, password))
                db.session.commit()
        reception_username = app.config.get("RECEPTION_USERNAME")
        reception_password = app.config.get("RECEPTION_PASSWORD")
        if reception_username and reception_password:
            if len(reception_password) < 12:
                raise RuntimeError("RECEPTION_PASSWORD deve ter pelo menos 12 caracteres.")
            if not Admin.query.filter_by(username=reception_username).first():
                db.session.add(
                    Admin.create(reception_username, reception_password, role="reception")
                )
                db.session.commit()
        print("Banco inicializado.")

    @app.errorhandler(413)
    def request_too_large(_error):
        return jsonify({"error": "A imagem excede o limite de 5 MB."}), 413

    return app

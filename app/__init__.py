# -*- coding: utf-8 -*-
"""
    app
    ~~~

    Provides the flask application
"""
from functools import partial
from os import path as p, getenv
from pathlib import Path
from pickle import DEFAULT_PROTOCOL
from logging import WARNING

from flask import Flask
from flask.logging import default_handler
from flask_cors import CORS
from flask_caching import Cache
from flask_compress import Compress

import config
import pygogo as gogo

from rq_dashboard import default_settings
from rq_dashboard.cli import add_basic_auth

from app.helpers import email_hdlr, flask_formatter

from mezmorize.utils import get_cache_config, get_cache_type
from meza.fntools import CustomEncoder

__version__ = "0.2.0"
__title__ = "COVID19 IL Data API"
__package_name__ = config.__APP_NAME__
__author__ = config.__AUTHOR__
__description__ = "COVID19 IL Data API"
__author_email__ = config.__AUTHOR_EMAIL__
__license__ = "MIT"
__copyright__ = "Copyright 2020 Nerevu Group"

BASEDIR = p.dirname(__file__)

cache = Cache()
compress = Compress()
cors = CORS()

logger = gogo.Gogo(__name__, monolog=True).logger


def register_rq(app):
    username = app.config.get("RQ_DASHBOARD_USERNAME")
    password = app.config.get("RQ_DASHBOARD_PASSWORD")

    if username and password:
        add_basic_auth(blueprint=rq, username=username, password=password)
        app.logger.info(f"Creating RQ-dashboard login for {username}")

    app.register_blueprint(rq, url_prefix="/dashboard")


def configure_talisman(app):
    if app.config.get("TALISMAN"):
        from flask_talisman import Talisman

        talisman_kwargs = {
            k.replace("TALISMAN_", "").lower(): v
            for k, v in app.config.items()
            if k.startswith("TALISMAN_")
        }

        Talisman(app, **talisman_kwargs)


def configure_cache(app):
    if app.config.get("PROD_SERVER") or app.config.get("DEBUG_MEMCACHE"):
        cache_type = get_cache_type(spread=False)
        cache_dir = None
    else:
        cache_type = "filesystem"
        parent_dir = Path(p.dirname(BASEDIR))
        cache_dir = parent_dir.joinpath(".cache", f"v{DEFAULT_PROTOCOL}")

    message = f"Set cache type to {cache_type}"
    cache_config = get_cache_config(cache_type, CACHE_DIR=cache_dir, **app.config)

    if cache_config["CACHE_TYPE"] == "filesystem":
        message += f" in {cache_config['CACHE_DIR']}"

    app.logger.debug(message)
    cache.init_app(app, config=cache_config)

    # TODO: keep until https://github.com/sh4nks/flask-caching/issues/113 is solved
    DEF_TIMEOUT = app.config.get("CACHE_DEFAULT_TIMEOUT")
    timeout = app.config.get("SET_TIMEOUT", DEF_TIMEOUT)
    cache.set = partial(cache.set, timeout=timeout)


def set_settings(app):
    required_settings = app.config.get("REQUIRED_SETTINGS", [])
    required_prod_settings = app.config.get("REQUIRED_PROD_SETTINGS", [])
    settings = required_settings + required_prod_settings

    for setting in settings:
        app.config.setdefault(setting, getenv(setting))


def check_settings(app):
    required_setting_missing = False

    for setting in app.config.get("REQUIRED_SETTINGS", []):
        if not app.config.get(setting):
            required_setting_missing = True
            app.logger.error(f"App setting {setting} is missing!")

    if app.config.get("PROD_SERVER"):
        server_name = app.config.get("SERVER_NAME")

        if server_name:
            app.logger.info(f"SERVER_NAME is {server_name}.")
        else:
            app.logger.error("SERVER_NAME is not set!")

        for setting in app.config.get("REQUIRED_PROD_SETTINGS", []):
            if not app.config.get(setting):
                required_setting_missing = True
                app.logger.error(f"Production app setting {setting} is missing!")
    else:
        app.logger.info("Production server not detected.")

    if not required_setting_missing:
        app.logger.info("All required app settings present!")

    for setting in app.config.get("OPTIONAL_SETTINGS", []):
        if not app.config.get(setting):
            app.logger.warning(f"Optional app setting {setting} is missing!")

    return required_setting_missing


def create_app(config_mode=None, config_file=None):
    app = Flask(__name__)
    app.url_map.strict_slashes = False
    cors.init_app(app)
    compress.init_app(app)

    app.config.from_object(default_settings)

    if config_mode:
        app.config.from_object(getattr(config, config_mode))
    elif config_file:
        app.config.from_pyfile(config_file)
    else:
        app.config.from_envvar("APP_SETTINGS", silent=True)

    set_settings(app)

    if not app.debug:
        email_hdlr.setLevel(WARNING)
        email_hdlr.setFormatter(flask_formatter)
        app.logger.addHandler(email_hdlr)

    default_handler.setFormatter(flask_formatter)
    app.register_blueprint(housekeeping)
    check_settings(app)

    app.register_blueprint(api)
    register_rq(app)
    configure_talisman(app)
    configure_cache(app)
    app.json_encoder = CustomEncoder
    return app


# put at bottom to avoid circular reference errors
from app.api import blueprint as api  # noqa
from app.housekeeping import blueprint as housekeeping  # noqa
from rq_dashboard import blueprint as rq  # noqa
